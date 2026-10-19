"""The hourly sync: what Yurtiçi says about the parcels it is carrying.

A shipment registered through the web service has no tracking number until
the branch scans its label; this fills it in, with the tracking page, and
keeps the last status (collected, on its way, delivered) on the order for the
admin to read. Delivered and cancelled shipments are not asked about again,
nor are any shipped more than SYNC_DAYS ago.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Carrier, Order, OrderStatus
from app.services import shipping
from app.services.shipping import yurtici

logger = logging.getLogger(__name__)

SYNC_DAYS = 45
#: Keys per queryShipment call.
BATCH = 50


async def sync_shipments(db: AsyncSession) -> int:
    """Ask Yurtiçi about every open shipment; returns how many it answered for."""
    provider = shipping.provider(Carrier.YURTICI)
    if provider is None:
        return 0
    now = datetime.now(timezone.utc)
    orders = (
        await db.scalars(
            select(Order).where(
                Order.status == OrderStatus.SHIPPED,
                Order.carrier == Carrier.YURTICI,
                Order.cargo_key.is_not(None),
                Order.shipped_at >= now - timedelta(days=SYNC_DAYS),
                Order.carrier_status.is_distinct_from(yurtici.DELIVERED),
                Order.carrier_status.is_distinct_from(yurtici.CANCELLED),
            )
        )
    ).all()
    by_key = {o.cargo_key: o for o in orders}
    keys = list(by_key)
    updated = 0
    for start in range(0, len(keys), BATCH):
        try:
            statuses = await provider.query(keys[start : start + BATCH])
        except shipping.ShippingError as exc:
            logger.warning("Yurtiçi sync failed: %s", exc)
            break
        for status in statuses:
            order = by_key.get(status.cargo_key)
            if order is not None:
                shipping.apply_status(order, status, now)
                updated += 1
    await db.commit()
    return updated
