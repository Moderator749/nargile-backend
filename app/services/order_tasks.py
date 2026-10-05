"""Background tasks for order lifecycle enforcement.

48-hour SLA: Bank-transfer orders still in PENDING_APPROVAL after 48 hours
are automatically cancelled and their reserved stock is released back to
the correct warehouse (RETAIL_STOCK or B2B_STOCK based on user segment).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import Inventory, WarehouseType
from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import SegmentType, User

logger = logging.getLogger(__name__)

SLA_HOURS = 48


def _warehouse_for_user(user: User) -> WarehouseType:
    """Determine which warehouse a user's stock was deducted from."""
    if user.segment and user.segment.code in (
        SegmentType.WHOLESALE,
        SegmentType.DEALER,
    ):
        return WarehouseType.B2B_STOCK
    return WarehouseType.RETAIL_STOCK


async def cancel_expired_pending_orders(db: AsyncSession) -> int:
    """Cancel bank-transfer orders that exceeded the 48-hour SLA.

    Returns the number of orders cancelled.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SLA_HOURS)

    result = await db.execute(
        select(Order)
        .where(
            Order.status == OrderStatus.PENDING_APPROVAL,
            Order.payment_method == PaymentMethod.BANK_TRANSFER,
            Order.created_at <= cutoff,
        )
        .options(selectinload(Order.items))
    )
    expired_orders = result.scalars().all()

    if not expired_orders:
        logger.info("SLA check: no expired pending orders found.")
        return 0

    # Pre-fetch all users for these orders (with their segments)
    user_ids = {o.user_id for o in expired_orders}
    user_result = await db.execute(
        select(User)
        .where(User.id.in_(user_ids))
        .options(selectinload(User.segment))
    )
    users_by_id = {u.id: u for u in user_result.scalars().all()}

    cancelled_count = 0

    for order in expired_orders:
        user = users_by_id.get(order.user_id)
        if user is None:
            logger.warning(
                "SLA: skipping order %s — user %s not found",
                order.order_number, order.user_id,
            )
            continue

        warehouse = _warehouse_for_user(user)

        # Restore inventory for each line item
        for item in order.items:
            inv_result = await db.execute(
                select(Inventory).where(
                    Inventory.variant_id == item.variant_id,
                    Inventory.warehouse_type == warehouse,
                )
            )
            inv = inv_result.scalar_one_or_none()

            if inv is not None:
                inv.quantity += item.quantity
            else:
                # Inventory row was deleted — recreate it
                db.add(Inventory(
                    variant_id=item.variant_id,
                    warehouse_type=warehouse,
                    quantity=item.quantity,
                ))

        order.status = OrderStatus.CANCELLED
        cancelled_count += 1

        logger.info(
            "SLA: cancelled order %s (created %s, user %s, warehouse %s)",
            order.order_number, order.created_at, user.email, warehouse.value,
        )

    await db.commit()
    logger.info("SLA check complete: %d order(s) cancelled.", cancelled_count)
    return cancelled_count
