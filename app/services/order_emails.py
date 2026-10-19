"""Customer-facing order e-mails (Turkish), rendered as text + HTML.

Rendering lives apart from delivery (app.services.mailer) so the wording can be
read and tested without a mail server.
"""

from decimal import Decimal

from app.core.config import settings
from app.models.order import Carrier, Order, ShippingMethod, ShippingPayer
from app.services.mailer import Mail

# One inline style per element: mail clients ignore stylesheets.
_WRAP = "font-family:Arial,Helvetica,sans-serif;color:#1d1d1f;line-height:1.6;max-width:600px"
_MUTED = "color:#6e6e73;font-size:13px"
_TABLE = "width:100%;border-collapse:collapse;font-size:14px;margin:16px 0"
_TH = "text-align:left;border-bottom:2px solid #1d1d1f;padding:6px 4px;font-size:12px;text-transform:uppercase"
_TD = "border-bottom:1px solid #e8e8ed;padding:6px 4px"


def _money(amount: Decimal | str) -> str:
    """₺1.234,56 — Turkish grouping, written out without a locale dependency."""
    whole, _, cents = f"{Decimal(amount):.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"₺{grouped},{cents}"


def order_shipped(
    order: Order,
    customer_name: str | None,
    to_email: str,
    lines: list[tuple[str, int]],
) -> Mail:
    """"Siparişiniz kargoya verildi": the dispatch notice.

    `lines` are (product name, quantity) pairs, resolved by the caller.
    """
    greeting = f"Sayın {customer_name}," if customer_name else "Sayın müşterimiz,"
    address = order.shipping_address or {}
    where = ", ".join(
        part for part in (address.get("line"), f"{address.get('district', '')} / {address.get('city', '')}".strip(" /")) if part
    )
    tracking = order.tracking_code
    link = order.tracking_url
    carrier = {"YURTICI": "Yurtiçi Kargo"}.get(str(getattr(order.carrier, "value", order.carrier) or ""), order.carrier_name)
    # The shop's own İstanbul van: no carrier, no tracking.
    courier = order.shipping_method == ShippingMethod.ISTANBUL_FREE or order.carrier == Carrier.OWN
    carrier_line = (
        "Siparişiniz İstanbul içi kurye ile yola çıktı; kurye teslimat öncesi sizi arayacaktır."
        if courier
        else "Siparişiniz kargoya verilmiştir, yola çıktı."
    )
    payment_note = (
        "Gönderim ücreti, teslimat sırasında kargo görevlisine tarafınızdan ödenecektir "
        "(karşı ödemeli). Sipariş tutarınıza kargo ücreti eklenmemiştir."
        if order.shipping_payer == ShippingPayer.RECEIVER
        else "Gönderim ücreti tarafımızca karşılanmıştır; teslimatta ödeme yapmanız gerekmez."
    )

    text = "\n".join(
        [
            greeting,
            "",
            carrier_line,
            "",
            f"Sipariş No: {order.order_number}",
            *( [f"Kargo Firması: {carrier}"] if carrier and not courier else [] ),
            *( [f"Kargo Takip No: {tracking}"] if tracking else [] ),
            *( [f"Kargonuzu takip edin: {link}"] if link else [] ),
            f"Sipariş Tutarı: {_money(order.total_amount - order.discount_amount)}",
            *( [f"Teslimat Adresi: {where}"] if where else [] ),
            "",
            "Sipariş içeriği:",
            *(f"  - {name} × {qty}" for name, qty in lines),
            "",
            payment_note,
            "",
            f"Bizi tercih ettiğiniz için teşekkür ederiz.\n{settings.SHOP_NAME}\n{settings.SHOP_URL}",
            f"Sorularınız için: {settings.SHOP_SUPPORT_PHONE}",
        ]
    )

    rows = "".join(
        f'<tr><td style="{_TD}">{name}</td><td style="{_TD};text-align:right">{qty}</td></tr>' for name, qty in lines
    )
    html = f"""\
<div style="{_WRAP}">
  <p>{greeting}</p>
  <p><strong>{carrier_line}</strong></p>
  <p style="{_MUTED}">
    Sipariş No: <strong>{order.order_number}</strong><br />
    {f"Kargo Firması: <strong>{carrier}</strong><br />" if carrier and not courier else ""}
    {f"Kargo Takip No: <strong>{tracking}</strong><br />" if tracking else ""}
    {f'<a href="{link}" style="color:#b4122b">Kargonuzu takip edin →</a><br />' if link else ""}
    Sipariş Tutarı: <strong>{_money(order.total_amount - order.discount_amount)}</strong>
    {f"<br />Teslimat Adresi: {where}" if where else ""}
  </p>
  <table style="{_TABLE}">
    <thead><tr><th style="{_TH}">Ürün</th><th style="{_TH};text-align:right">Adet</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="{_MUTED}">{payment_note}</p>
  <p>Bizi tercih ettiğiniz için teşekkür ederiz.<br />
    <strong>{settings.SHOP_NAME}</strong><br />
    <a href="{settings.SHOP_URL}" style="color:#b4122b">{settings.SHOP_URL}</a><br />
    <span style="{_MUTED}">Sorularınız için: {settings.SHOP_SUPPORT_PHONE}</span>
  </p>
</div>"""

    return Mail(
        to=to_email,
        subject=f"Siparişiniz kargoya verildi · {order.order_number}",
        text=text,
        html=html,
    )
