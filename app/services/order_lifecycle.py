"""Order state changes shared by the admin API and the SLA job."""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant
from app.models.order import Carrier, Order, OrderStatus, PaymentMethod
from app.models.user import User
from app.services.checkout_engine import CENTRAL_STOCK, stock_on_hand
from app.services.wallet import award_for_paid_order


async def mark_paid(db: AsyncSession, order: Order, now: datetime) -> tuple[int, int]:
    """Approve / mark paid, then grant Sultan Puan. Returns (standard, annual bonus)."""
    order.status = OrderStatus.APPROVED
    order.paid_at = now
    await db.flush()
    return await award_for_paid_order(db, order, now)


async def mark_shipped(
    db: AsyncSession,
    order: Order,
    tracking_code: str | None,
    *,
    carrier: Carrier | None = None,
    carrier_name: str | None = None,
    tracking_url: str | None = None,
    now: datetime | None = None,
) -> None:
    """Hand the order to the carrier. `tracking_code` is optional (the shop's
    own van has none, and a web-service shipment gets one once it is scanned).
    The dispatch e-mail is sent by the caller."""
    order.status = OrderStatus.SHIPPED
    order.shipped_at = now or datetime.now(timezone.utc)
    if carrier is not None:
        order.carrier = carrier
    order.carrier_name = (carrier_name or "").strip()[:100] or None
    if tracking_code:
        order.tracking_code = tracking_code[:100]
    order.tracking_url = tracking_url[:300] if tracking_url else None
    await db.flush()


async def cancel(db: AsyncSession, order: Order, user: User, reason: str, now: datetime) -> None:
    """Cancel and release the reserved stock to the warehouse it came from.

    Points settle themselves: the wallet ignores both the SPENT rows of a
    cancelled order (so spent points come back) and its EARNED rows (so points
    granted when it was paid go away). `order.items` and `user.segment` must be
    loaded.
    """
    await _release(db, order, user, reason, now, OrderStatus.CANCELLED)


async def fail_payment(db: AsyncSession, order: Order, user: User, reason: str, now: datetime) -> None:
    """A card payment that failed or was never completed: everything the order
    held goes back, as on a cancellation, but the order says why
    (PAYMENT_FAILED) so nobody mistakes it for one the shop turned down."""
    await _release(db, order, user, reason, now, OrderStatus.PAYMENT_FAILED)


async def deliver_in_store(db: AsyncSession, order: Order, now: datetime) -> None:
    """"Mağazadan Teslim": the customer collected it at the shop. Delivered
    there and then, with no carrier and nothing to track."""
    order.status = OrderStatus.DELIVERED
    order.carrier = Carrier.STORE
    order.carrier_name = None
    order.tracking_code = order.tracking_url = order.cargo_key = None
    order.carrier_status = order.carrier_status_text = order.carrier_synced_at = None
    order.shipped_at = order.delivered_at = now
    await db.flush()


async def return_order(
    db: AsyncSession, order: Order, reason: str, now: datetime, *, restock: bool
) -> None:
    """İade: an order that had left came back. Its points settle as on a
    cancellation (RETURNED is void in the wallet); the goods go back on the
    shelf unless `restock` is off (damaged, kept by the carrier). `order.items`
    must be loaded."""
    if restock:
        await _put_back(db, order)
    order.status = OrderStatus.RETURNED
    order.returned_at = now
    order.return_reason = reason[:300]
    await db.flush()


class StockShortage(Exception):
    """A closed order's goods have been sold since: it cannot take them back."""


def recovered_status(order: Order) -> OrderStatus:
    """Where "Siparişi Kurtar" puts an unpaid order: waiting for the transfer it
    is waiting for, or for an admin (a card order, whose payment page is long
    closed, and a quote request)."""
    if order.payment_method == PaymentMethod.BANK_TRANSFER and not order.is_quote:
        return OrderStatus.AWAITING_TRANSFER
    return OrderStatus.PENDING_APPROVAL


async def recover(db: AsyncSession, order: Order, now: datetime, *, paid: bool) -> tuple[int, int]:
    """"Siparişi Kurtar": bring a cancelled order back — paid (APPROVED, the
    money arrived after it was cancelled) or waiting for payment again.

    Its stock is taken again first (see `_take_stock_again`). The wallet counts
    its points again by itself (the order is no longer void). The 48-hour
    transfer clock starts again from now (see order_tasks). Returns the points
    granted, as `mark_paid` does. `order.items` must be loaded.
    """
    await _take_stock_again(db, order)
    order.recovered_at = now
    if not paid:
        order.status = recovered_status(order)
        await db.flush()
        return 0, 0
    if order.paid_at is not None:
        # Paid before it was cancelled: its points come back with it.
        order.status = OrderStatus.APPROVED
        await db.flush()
        return 0, 0
    return await mark_paid(db, order, now)


# "WhatsApp ile Kurtar": what the shop gives up of a failed card payment's
# total — the virtual POS commission it no longer pays — to have it by transfer.
RECOVERY_DISCOUNT_RATE = Decimal("0.05")


def payable(order: Order) -> Decimal:
    """What the customer pays for the order: its goods, less its discounts, plus carriage."""
    return (order.total_amount + order.shipping_cost - order.discount_amount).quantize(Decimal("0.01"))


async def recover_to_transfer(db: AsyncSession, order: Order, now: datetime) -> Decimal:
    """"WhatsApp ile Kurtar": a failed card order (PAYMENT_FAILED) back as a
    bank transfer at 5% off — the "Kurtarma İndirimi" — waiting for the money
    (AWAITING_TRANSFER) with a fresh 48 hours.

    Its stock is taken again first; when a line's goods have been sold since,
    nothing changes and StockShortage says which. The discount is 5% of what
    the customer was to pay, rounded half up to the kuruş, and goes into
    `discount_amount` (which every total reads) as well as `recovery_discount`.
    Its points count again by themselves, as on "Siparişi Kurtar". Returns the
    discount. `order.items` must be loaded; the caller checks the status.
    """
    await _take_stock_again(db, order)
    discount = (payable(order) * RECOVERY_DISCOUNT_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    order.discount_amount += discount
    order.recovery_discount = discount
    order.payment_method = PaymentMethod.BANK_TRANSFER
    order.status = OrderStatus.AWAITING_TRANSFER
    order.recovered_at = now
    await db.flush()
    return discount


def can_revert_recovery(order: Order) -> str | None:
    """Why "Kurtarmayı Geri Al" cannot undo this order's recovery, or None when it can."""
    if order.status != OrderStatus.AWAITING_TRANSFER or not order.recovery_discount:
        return "Yalnızca WhatsApp ile kurtarılmış ve havale bekleyen bir siparişin kurtarması geri alınabilir"
    if order.paid_at is not None:
        return "Sipariş ödenmiş; kurtarma geri alınamaz"
    if order.store_approved_at is not None:
        return "Mağaza onayı verilmiş; önce mağaza onayını geri alın"
    return None


async def revert_recovery(db: AsyncSession, order: Order) -> None:
    """"Kurtarmayı Geri Al": undo "WhatsApp ile Kurtar" — the customer turned
    the transfer down, or the message was never sent. Back to the failed card
    payment it was: its goods on the shelf again, the "Kurtarma İndirimi" off
    `discount_amount`, CREDIT_CARD, PAYMENT_FAILED, and no recovery on record
    (so it can be recovered again). Why the card failed was kept all along.
    The caller checks `can_revert_recovery`; `order.items` must be loaded.
    """
    await _put_back(db, order)
    order.discount_amount -= order.recovery_discount
    order.recovery_discount = Decimal("0.00")
    order.payment_method = PaymentMethod.CREDIT_CARD
    order.status = OrderStatus.PAYMENT_FAILED
    order.recovered_at = None
    await db.flush()


async def _take_stock_again(db: AsyncSession, order: Order) -> None:
    """The goods of an order that came to nothing, taken out of stock again,
    under the same lock as checkout. When any line's goods have been sold
    since, StockShortage names every such line, and the caller rolls back
    whatever the lines before it took."""
    short: list[str] = []
    for item in order.items:
        if item.stock_warehouse is None and order.is_quote:
            continue
        rows = (
            await db.scalars(
                select(Inventory)
                .where(Inventory.variant_id == item.variant_id, Inventory.warehouse_type == CENTRAL_STOCK)
                .with_for_update()
            )
        ).all()
        available = stock_on_hand(rows)
        if available < item.quantity:
            short.append(f"{await _line_name(db, item.variant_id)}: {item.quantity} adet gerekli, stokta {available}")
            continue
        rows[0].quantity -= item.quantity
        item.stock_warehouse = CENTRAL_STOCK
    if short:
        raise StockShortage("; ".join(short))


async def _line_name(db: AsyncSession, variant_id) -> str:
    row = (
        await db.execute(
            select(Product.name, ProductVariant.color_name)
            .join(ProductVariant, ProductVariant.product_id == Product.id)
            .where(ProductVariant.id == variant_id)
        )
    ).first()
    if row is None:
        return "Katalogdan kaldırılmış ürün"
    return row.name + (f" ({row.color_name})" if row.color_name else "")


async def _put_back(db: AsyncSession, order: Order) -> None:
    """Every line's goods back to the central pool."""
    for item in order.items:
        # A request took nothing out of stock. Every other line goes back to the
        # central pool — one taken from a since-retired segment quota included,
        # as the shop now has a single stock.
        if item.stock_warehouse is None and order.is_quote:
            continue
        # Locked like checkout's read, so a sale at the same moment cannot
        # overwrite what is put back.
        inv = await db.scalar(
            select(Inventory)
            .where(Inventory.variant_id == item.variant_id, Inventory.warehouse_type == CENTRAL_STOCK)
            .with_for_update()
        )
        if inv is not None:
            inv.quantity += item.quantity
        else:  # inventory row was deleted: recreate it
            db.add(Inventory(variant_id=item.variant_id, warehouse_type=CENTRAL_STOCK, quantity=item.quantity))


async def _release(
    db: AsyncSession, order: Order, user: User, reason: str, now: datetime, status: OrderStatus
) -> None:
    await _put_back(db, order)
    order.status = status
    order.cancelled_at = now
    order.cancel_reason = reason[:300]
