"""The customer's SMS about their order: "Sipariş alındı" once it is placed,
"Kargoya verildi" once it leaves. Texts and switches are "SMS Ayarları"
(app.services.sms_settings).

Sent after the request has answered, in a session of its own, so a slow or
failing gateway never holds up a checkout or a dispatch, and never raises:
whatever happens is in the notification log (SMS & Mail › Geçmiş) or the server
log. When an order is "placed": at checkout for a bank transfer or a quote; a
card order only once PayTR reports it paid, so nobody is thanked for an order
whose payment then fails.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.order import Carrier, Order
from app.models.user import User
from app.services import sms_settings
from app.services.notify import dispatch
from app.services.notify.phone import normalize_tr_mobile

logger = logging.getLogger("notify")

CARRIER_NAMES = {Carrier.YURTICI: "Yurtiçi Kargo", Carrier.OWN: "Mağaza aracı"}


def _phone(order: Order, user: User) -> str | None:
    """The account's mobile; else the one on the delivery address or the receiver's."""
    for raw in (user.phone, (order.shipping_address or {}).get("phone"), order.receiver_phone):
        number = normalize_tr_mobile(raw)
        if number:
            return number
    return None


def values(order: Order, user: User) -> dict[str, str]:
    """What the placeholders say for this order."""
    name = (user.full_name or (order.shipping_address or {}).get("full_name") or "").strip()
    carrier = CARRIER_NAMES.get(order.carrier) if order.carrier else None
    return {
        "Name": name or "Değerli müşterimiz",
        "OrderNo": order.order_number,
        # A web-service shipment has no carrier number until its label is
        # scanned: the link follows it meanwhile, and the shop's own van has
        # neither, so the order number stands in (it works on /kargo-takip).
        "Tracking": order.tracking_code or order.tracking_url or order.order_number,
        "Carrier": carrier or order.carrier_name or order.freight_company_name or "kargo",
    }


async def notify(factory: async_sessionmaker, order_id: uuid.UUID, event: str) -> None:
    """Send `event`'s SMS for an order, if it is switched on and the customer has a mobile."""
    try:
        async with factory() as db:
            order = await db.get(Order, order_id)
            user = await db.get(User, order.user_id) if order else None
            if order is None or user is None:
                return
            config = await sms_settings.load(db)
            template = config.templates[event]
            if not template.enabled:
                return
            number = _phone(order, user)
            if number is None:
                logger.info("No mobile for %s SMS of order %s", event, order.order_number)
                return
            await dispatch.send_sms(
                db,
                recipients=[(number, user.id)],
                text=sms_settings.render(template.text, values(order, user)),
                purpose=event,
                meta={"order_id": str(order.id), "order_number": order.order_number},
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — an SMS must never surface as an order error
        logger.exception("%s SMS for order %s failed", event, order_id)
