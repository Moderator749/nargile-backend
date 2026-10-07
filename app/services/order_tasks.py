"""Background tasks for order lifecycle enforcement.

48-hour SLA: bank-transfer orders still awaiting the transfer after 48 hours
are cancelled; their reserved stock goes back to the warehouse it came from
and any points spent on them return (see order_lifecycle.cancel).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import User
from app.services import order_lifecycle

logger = logging.getLogger(__name__)

SLA_HOURS = 48


async def cancel_expired_pending_orders(db: AsyncSession) -> int:
    """Cancel bank-transfer orders that exceeded the 48-hour SLA.

    Returns the number of orders cancelled.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=SLA_HOURS)

    result = await db.execute(
        select(Order)
        .where(
            Order.status.in_((OrderStatus.AWAITING_TRANSFER, OrderStatus.PENDING_APPROVAL)),
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

        await order_lifecycle.cancel(db, order, user, "48 saat içinde havale alınmadı (otomatik iptal)", now)
        cancelled_count += 1

        logger.info(
            "SLA: cancelled order %s (created %s, user %s)", order.order_number, order.created_at, user.email
        )

    await db.commit()
    logger.info("SLA check complete: %d order(s) cancelled.", cancelled_count)
    return cancelled_count
