"""Order state changes shared by the admin API and the SLA job."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.checkout_engine import warehouse_for_segment
from app.services.wallet import award_for_paid_order


async def mark_paid(db: AsyncSession, order: Order, now: datetime) -> tuple[int, int]:
    """Approve / mark paid, then grant Sultan Puan. Returns (standard, annual bonus)."""
    order.status = OrderStatus.APPROVED
    order.paid_at = now
    await db.flush()
    return await award_for_paid_order(db, order, now)


async def mark_shipped(db: AsyncSession, order: Order, tracking_code: str | None) -> None:
    """Hand the order to the carrier. `tracking_code` is optional (courier runs
    have none). The dispatch e-mail is sent by the caller."""
    order.status = OrderStatus.SHIPPED
    if tracking_code:
        order.tracking_code = tracking_code[:100]
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
    # Lines from before the source was recorded go back to the buyer's quota.
    legacy = warehouse_for_segment(user.segment.code if user.segment else None)
    for item in order.items:
        warehouse = item.stock_warehouse or (None if order.is_quote else legacy)
        if warehouse is None:  # a request took nothing out of stock
            continue
        inv = await db.scalar(
            select(Inventory).where(Inventory.variant_id == item.variant_id, Inventory.warehouse_type == warehouse)
        )
        if inv is not None:
            inv.quantity += item.quantity
        else:  # inventory row was deleted: recreate it
            db.add(Inventory(variant_id=item.variant_id, warehouse_type=warehouse, quantity=item.quantity))
    order.status = status
    order.cancelled_at = now
    order.cancel_reason = reason[:300]
