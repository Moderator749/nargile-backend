"""What every carrier integration offers, whatever it speaks underneath.

A provider registers a shipment under our own reference (the cargo key the
label's barcode carries), reports how each one is getting on, and withdraws one
that has not been collected yet. A carrier with no web service has no provider:
its tracking code is typed in by hand ("Kargola") and nothing is asked of it.
"""

from dataclasses import dataclass
from typing import Protocol


class ShippingError(Exception):
    """The carrier refused the request, or could not be reached; the message is
    the carrier's own reason, fit to show an admin."""


@dataclass(frozen=True)
class ShipmentRequest:
    """One parcel to register, addressed as its label is."""

    cargo_key: str
    receiver_name: str
    address: str
    city: str
    district: str
    phone: str
    email: str | None = None
    pieces: int = 1
    note: str | None = None


@dataclass(frozen=True)
class ShipmentStatus:
    """What the carrier says about one shipment."""

    cargo_key: str
    # The carrier's own short code ("NOP", "IND", "DLV", "CNL"…) and words.
    code: str
    text: str
    # The carrier's tracking number and page, once it has scanned the parcel.
    tracking_number: str | None = None
    tracking_url: str | None = None
    delivered: bool = False
    cancelled: bool = False
    # Set when the carrier knows nothing of this cargo key.
    error: str | None = None


class ShippingProvider(Protocol):
    async def create(self, shipment: ShipmentRequest) -> None:
        """Register the shipment; ShippingError if the carrier refuses it."""

    async def query(self, cargo_keys: list[str]) -> list[ShipmentStatus]:
        """The status of each shipment, in the order asked."""

    async def cancel(self, cargo_key: str) -> ShipmentStatus:
        """Withdraw a shipment the carrier has not collected yet."""
