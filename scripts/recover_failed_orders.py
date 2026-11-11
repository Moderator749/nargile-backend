"""Failed card payments worth a call: who, how much, why, and a WhatsApp link.

    venv/bin/python scripts/recover_failed_orders.py              # last 30 days, still lost
    venv/bin/python scripts/recover_failed_orders.py --days 7
    venv/bin/python scripts/recover_failed_orders.py --all        # also: bought later, test mode
    venv/bin/python scripts/recover_failed_orders.py --mask       # names/phones masked, no links (to share or paste)

Lists PAYMENT_FAILED card orders, newest first, with the reason PayTR gave
(read as the admin panel reads it: services/payment_failures) and a
click-to-chat link with a message written for that reason.

Left out unless --all:
- the customer placed a later order that went through (paid, shipped or
  waiting for their transfer): most "failures" are a first attempt abandoned
  and redone a few minutes later, and that customer has already bought;
- PayTR test-mode payments.

Read-only: it changes no order. The message offers Havale/EFT at the
"WhatsApp ile Kurtar" discount (5%, order_lifecycle.RECOVERY_DISCOUNT_RATE);
when the customer takes it, press "WhatsApp ile Kurtar" on the order in the
admin panel, which makes it a transfer at that price and holds its stock again.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Product, ProductVariant  # noqa: E402
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod  # noqa: E402
from app.models.payment import PaymentTransaction  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import payment_failures  # noqa: E402
from app.services.order_lifecycle import RECOVERY_DISCOUNT_RATE, payable  # noqa: E402

ISTANBUL = ZoneInfo("Europe/Istanbul")
MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
#: A later order in one of these means the customer bought after all.
BOUGHT = (
    OrderStatus.APPROVED, OrderStatus.SHIPPED, OrderStatus.DELIVERED,
    OrderStatus.AWAITING_TRANSFER, OrderStatus.RETURNED,
)


def tl(amount: Decimal) -> str:
    """1234.5 → "1.234,50 TL", as the shop writes money."""
    whole, frac = f"{amount.quantize(Decimal('0.01')):,.2f}".split(".")
    return f"{whole.replace(',', '.')},{frac} TL"


def whatsapp_number(raw: str | None) -> str | None:
    """As the admin panel dials (saas_frontend lib/whatsapp.ts): Turkish mobiles to 90…, others as written."""
    given = (raw or "").strip()
    digits = re.sub(r"\D", "", given)
    if given.startswith("+") or digits.startswith("00"):
        international = digits[2:] if digits.startswith("00") else digits
        return international if len(international) >= 8 else None
    if len(digits) == 11 and digits.startswith("05"):
        return "90" + digits[1:]
    if len(digits) == 10 and digits.startswith("5"):
        return "90" + digits
    if len(digits) == 12 and digits.startswith("905"):
        return digits
    return None


def placed_when(at: datetime, now: datetime) -> str:
    """"Bugün", "Dün" or "3 Kasım tarihinde", on İstanbul's clock."""
    local, today = at.astimezone(ISTANBUL), now.astimezone(ISTANBUL)
    days = (today.date() - local.date()).days
    if days <= 0:
        return "Bugün"
    if days == 1:
        return "Dün"
    return f"{local.day} {MONTHS[local.month - 1]} tarihinde"


def mask_name(name: str) -> str:
    return " ".join(part[:1] + "***" for part in name.split()) or "-"


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"…{digits[-2:]}" if digits else "-"


@dataclass
class Row:
    order: Order
    name: str
    phone: str | None
    items: list[tuple[str, int]]
    failure: payment_failures.PaymentFailure | None
    test_mode: bool
    bought_later: Order | None

    @property
    def amount(self) -> Decimal:
        return payable(self.order)

    @property
    def kind(self) -> str:
        """abandoned | bank | store: how the message speaks."""
        f = self.failure
        if f is None or f.code == "6" or (f.message or "").find("tamamlanmadı") >= 0:
            return "abandoned"
        return "bank" if f.source == payment_failures.FailureSource.CUSTOMER_BANK else "store"

    @property
    def reason(self) -> str:
        f = self.failure
        if f is None:
            return "-"
        label = {"abandoned": "Ödeme sayfası tamamlanmadı", "bank": "Banka onaylamadı", "store": "Mağaza/PayTR tarafı"}[self.kind]
        return f"{label} ({f.code})" if f.code else label


def message(row: Row, now: datetime) -> str:
    """The WhatsApp text: what happened, in the customer's terms, and two ways on."""
    first = row.name.split()[0] if row.name and row.name != "-" else ""
    before = row.amount
    after = before - (before * RECOVERY_DISCOUNT_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    when = placed_when(row.order.created_at, now)
    number = row.order.order_number
    if row.kind == "abandoned":
        what = (
            f"{when} oluşturduğunuz {number} numaralı siparişinizin kartla ödeme adımı tamamlanmamış görünüyor. "
            "Ödeme sırasında bir sorun yaşadıysanız memnuniyetle yardımcı olalım."
        )
    elif row.kind == "bank":
        said = (row.failure.message or "").strip().rstrip(".") if row.failure else ""
        what = (
            f"{when} oluşturduğunuz {number} numaralı siparişinizde kart ödemesi banka tarafından onaylanmadı"
            + (
                f" (bankanın yanıtı: \"{said}\")."
                if said
                else ". Bu genellikle kart limiti, internet alışverişi izni ya da 3D Secure doğrulamasından kaynaklanır."
            )
        )
    else:
        what = (
            f"{when} oluşturduğunuz {number} numaralı siparişinizde ödeme bizim tarafımızdaki bir sorun nedeniyle "
            "tamamlanamadı; yaşattığımız aksaklık için özür dileriz."
        )
    return "\n\n".join([
        f"Merhaba{(' ' + first) if first else ''}, Nargile Malzemesi'nden ulaşıyorum.",
        what,
        "Siparişinizi tamamlamak için iki kolay yol sunabiliriz:\n"
        f"• *Havale/EFT* ile ödemek isterseniz Sanal POS komisyonunu size indirim olarak yansıtıyoruz: tutar {tl(before)} yerine *{tl(after)}* olur.\n"
        "• *Kartla* yeniden denemek isterseniz siparişinizi birlikte hemen yeniden oluşturabiliriz.",
        "Hangisi size uygun olursa yazmanız yeterli. İyi günler dileriz.",
    ])


async def load(days: int) -> list[Row]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as db:
        orders = list(
            await db.scalars(
                select(Order)
                .where(
                    Order.status == OrderStatus.PAYMENT_FAILED,
                    Order.payment_method == PaymentMethod.CREDIT_CARD,
                    Order.created_at >= since,
                )
                .order_by(Order.created_at.desc())
            )
        )
        if not orders:
            return []
        ids = [o.id for o in orders]
        users = {u.id: u for u in await db.scalars(select(User).where(User.id.in_({o.user_id for o in orders})))}
        txs: dict = defaultdict(list)
        for tx in await db.scalars(select(PaymentTransaction).where(PaymentTransaction.order_id.in_(ids))):
            txs[tx.order_id].append(tx)
        items: dict = defaultdict(list)
        for order_id, name, qty in await db.execute(
            select(OrderItem.order_id, Product.name, OrderItem.quantity)
            .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(OrderItem.order_id.in_(ids))
            .order_by(OrderItem.line_total.desc())
        ):
            items[order_id].append((name, qty))
        later: dict = {}
        for o in await db.scalars(
            select(Order)
            .where(Order.user_id.in_({o.user_id for o in orders}), Order.status.in_(BOUGHT), Order.created_at >= since)
            .order_by(Order.created_at)
        ):
            later.setdefault(o.user_id, []).append(o)

    rows = []
    for o in orders:
        user = users.get(o.user_id)
        address = o.shipping_address or {}
        name = (user.full_name if user else None) or address.get("full_name") or o.receiver_name or "-"
        phone = (user.phone if user else None) or address.get("phone") or o.receiver_phone
        bought = next((n for n in later.get(o.user_id, []) if n.created_at > o.created_at), None)
        rows.append(Row(
            order=o, name=name.strip(), phone=phone, items=items[o.id],
            failure=payment_failures.for_order(o, txs[o.id]),
            test_mode=any(t.test_mode for t in txs[o.id]),
            bought_later=bought,
        ))
    return rows


def print_table(rows: list[Row], mask: bool) -> None:
    head = ["#", "Sipariş", "Tarih", "Müşteri", "Telefon", "Tutar", "Neden", "Ürünler"]
    body = []
    for n, r in enumerate(rows, 1):
        products = ", ".join(f"{q}× {name}" for name, q in r.items[:2]) + (f" +{len(r.items) - 2}" if len(r.items) > 2 else "")
        body.append([
            str(n), r.order.order_number, r.order.created_at.astimezone(ISTANBUL).strftime("%d.%m %H:%M"),
            mask_name(r.name) if mask else r.name, mask_phone(r.phone) if mask else (r.phone or "-"),
            tl(r.amount), r.reason, products[:48],
        ])
    widths = [min(max(len(h), *(len(b[i]) for b in body)), 48) for i, h in enumerate(head)]
    line = lambda cells: "  ".join(c.ljust(w) if i != 5 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths)))  # noqa: E731
    print(line(head))
    print("  ".join("-" * w for w in widths))
    for b in body:
        print(line(b))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="how far back (default 30)")
    parser.add_argument("--all", action="store_true", help="also those who bought later, and test-mode payments")
    parser.add_argument("--mask", action="store_true", help="mask names and phones, print no links")
    args = parser.parse_args()

    async def go() -> list[Row]:
        try:
            return await load(args.days)
        finally:
            await engine.dispose()

    rows = asyncio.run(go())
    now = datetime.now(timezone.utc)
    test = [r for r in rows if r.test_mode]
    bought = [r for r in rows if not r.test_mode and r.bought_later]
    lost = [r for r in rows if not r.test_mode and not r.bought_later]
    total = lambda rs: tl(sum((r.amount for r in rs), Decimal("0")))  # noqa: E731

    print(f"Son {args.days} gün: {len(rows)} başarısız kart siparişi ({total(rows)})")
    print(f"  • sonradan başka siparişle satın alan: {len(bought)} ({total(bought)})")
    print(f"  • PayTR test modu: {len(test)} ({total(test)})")
    print(f"  • gerçekten kaybedilen: {len(lost)} ({total(lost)})")
    reasons = Counter(r.reason for r in lost)
    if reasons:
        print("  kaybedilenlerin nedenleri: " + "; ".join(f"{k}: {v}" for k, v in reasons.most_common()))

    shown = rows if args.all else lost
    if not shown:
        print("\nAranacak sipariş yok.")
        return
    print()
    print_table(shown, args.mask)

    if args.all:
        print("\n(--all: alıcı sonradan satın aldıysa ya da test ödemesiyse aşağıda bağlantı verilmez)")
    print("\nWhatsApp bağlantıları" + (" (--mask: gizlendi)" if args.mask else "") + ":")
    for n, r in enumerate(shown, 1):
        if r.test_mode or r.bought_later:
            note = "test ödemesi" if r.test_mode else f"sonradan satın aldı: {r.bought_later.order_number}"
            print(f"  {n}. {r.order.order_number}: — ({note})")
            continue
        number = whatsapp_number(r.phone)
        if not number:
            print(f"  {n}. {r.order.order_number}: — (WhatsApp'a uygun telefon yok)")
        elif args.mask:
            print(f"  {n}. {r.order.order_number}: https://wa.me/{number[:4]}…  [{r.kind}]")
        else:
            print(f"  {n}. {r.order.order_number}: https://wa.me/{number}?text={quote(message(r, now))}")
        if r.failure and not args.mask:
            print(f"     ekip notu: {r.failure.advice}")
    print(
        "\nMüşteri havaleyi kabul ederse: panelde siparişi açıp \"WhatsApp ile Kurtar\" ile %5 indirimli "
        "havaleye çevirin (stok yeniden ayrılır; ürün tükendiyse panel söyler)."
    )


if __name__ == "__main__":
    main()
