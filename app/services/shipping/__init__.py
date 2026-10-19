"""Shipping: the carriers an order can leave with, and the ones we can talk to.

``provider(carrier)`` is the carrier's web service when one is configured
(only Yurtiçi so far), else None — and then "Kargola" simply records the
tracking code typed in. ``tracking_url`` is where a customer follows a code.
"""

import re

from app.core.config import settings
from app.models.order import Carrier, Order, ShippingMethod
from app.services.shipping import yurtici
from app.services.shipping.base import ShipmentRequest, ShipmentStatus, ShippingError, ShippingProvider

__all__ = [
    "ShipmentRequest",
    "ShipmentStatus",
    "ShippingError",
    "ShippingProvider",
    "default_carrier",
    "next_cargo_key",
    "provider",
    "tracking_url",
]


def provider(carrier: Carrier | str | None) -> ShippingProvider | None:
    if carrier == Carrier.YURTICI and settings.yurtici_configured:
        return yurtici.YurticiProvider(
            settings.YURTICI_API_USER,
            settings.YURTICI_API_PASSWORD,
            language=settings.YURTICI_API_LANGUAGE,
            test=settings.YURTICI_TEST_MODE,
        )
    return None


def tracking_url(carrier: Carrier | str | None, code: str | None) -> str | None:
    """The carrier's own page for a tracking code; None where there is none to build."""
    if not code or not code.strip():
        return None
    if carrier == Carrier.YURTICI:
        return yurtici.tracking_url(code)
    return None


def default_carrier(order: Order) -> Carrier:
    """Who takes the parcel unless the admin says otherwise, from what was chosen at checkout."""
    if order.shipping_method == ShippingMethod.ISTANBUL_FREE:
        return Carrier.OWN
    if order.shipping_method in (ShippingMethod.NAKLIYE_AMBARI, ShippingMethod.INTERNATIONAL_FREIGHT):
        return Carrier.OTHER
    return Carrier.YURTICI


def next_cargo_key(order: Order) -> str:
    """Our reference for the order's shipment, as the label's barcode carries it.

    The order number, letters and digits only ("ORD-20261019085034-9E23B8" →
    "20261019085034-9E23B8" → "202610190850349E23B8"). A shipment withdrawn and
    registered again gets a new one ("…B", "…C"), since the carrier keeps the
    first on file as cancelled.
    """
    base = re.sub(r"[^A-Za-z0-9]", "", order.order_number.removeprefix("ORD-"))[:20].upper()
    if not order.cargo_key or not order.cargo_key.startswith(base):
        return base
    suffix = order.cargo_key[len(base):]
    return base + (chr(ord(suffix[-1]) + 1) if suffix else "B")


def shipment_request(order: Order, cargo_key: str, email: str | None) -> ShipmentRequest:
    """The order's parcel as the carrier is asked to carry it: to the person
    named to collect it if there is one, else to the address's own name."""
    address = order.shipping_address or {}
    note = f"Şubeden teslim: {order.shipping_branch_name}" if order.shipping_branch_name else None
    return ShipmentRequest(
        cargo_key=cargo_key,
        receiver_name=order.receiver_name or address.get("full_name") or "",
        address=address.get("line") or "",
        city=address.get("city") or "",
        district=address.get("district") or "",
        phone=order.receiver_phone or address.get("phone") or "",
        email=email,
        note=note,
    )


def apply_status(order: Order, status: ShipmentStatus, now) -> None:
    """Record what the carrier reported. Its tracking number fills the order's
    tracking code once the parcel has been scanned, unless one was typed in."""
    order.carrier_synced_at = now
    if status.error:
        order.carrier_status, order.carrier_status_text = "ERR", status.error[:200]
        return
    order.carrier_status = status.code[:10] or None
    order.carrier_status_text = status.text[:200] or None
    if status.tracking_number and not order.tracking_code:
        order.tracking_code = status.tracking_number[:100]
    if order.tracking_code and not order.tracking_url:
        order.tracking_url = (status.tracking_url or tracking_url(order.carrier, order.tracking_code) or "")[:300] or None
