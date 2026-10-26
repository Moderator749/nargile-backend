"""Order state changes shared by the admin API and the SLA job."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory
from app.models.order import Carrier, Order, OrderStatus
from app.models.user import User
from app.services.checkout_engine import CENTRAL_STOCK
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


async def _release(
    db: AsyncSession, order: Order, user: User, reason: str, now: datetime, status: OrderStatus
) -> None:
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
    order.status = status
    order.cancelled_at = now
    order.cancel_reason = reason[:300]
