"""Admin views of a registered account: the grid, one customer in full, and the
one thing an admin changes about them — which price list they buy from.

Sign-up collects an identity and an invoice; nothing in it grants a price tier.
This is where a tier is granted, so the shapes here are the read side of
registration plus :class:`SegmentAssign`.
"""

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.user import AccountType, AddressKind, BillingEntity, InternationalKind, SegmentType


class CustomerSort(str, enum.Enum):
    """Sortable columns of the admin customers table."""

    NAME = "name"  # Müşteri / unvan
    CREATED = "created"  # Kayıt tarihi
    ORDERS = "orders"


class AdminCustomerRead(BaseModel):
    """One row of the customers grid."""

    id: uuid.UUID
    email: str
    full_name: str | None
    # Ünvan, when the account is a firm's or is invoiced as one; the grid shows
    # it beside the name because it is what the shop knows them by.
    company_title: str | None = None
    account_type: AccountType
    phone: str | None = None
    segment: SegmentType | None
    order_count: int
    created_at: datetime
    # A guest checkout's buyer ("Misafir"), not an account.
    is_guest: bool = False
    # Where they are, from their invoice address: a Turkish row carries its
    # şehir and ilçe, a foreign one its country. Structured rather than
    # pre-formatted, so the grid decides how to say it.
    city: str | None = None
    district: str | None = None
    country: str | None = None


class CustomerAddressRead(BaseModel):
    """Either shape of stored address; the Turkish fields or the international
    ones are filled, never both — see ``CustomerAddress``."""

    model_config = ConfigDict(from_attributes=True)

    kind: AddressKind
    # A saved delivery address's own name ("Ev"); None on an invoice address.
    label: str | None = None
    country: str
    city: str
    neighbourhood: str | None = None
    street: str | None = None
    building_no: str | None = None
    building_name: str | None = None
    door_no: str | None = None
    district: str | None = None
    postal_code: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None


class CustomerProfileRead(BaseModel):
    """Everything sign-up asked beyond the login."""

    model_config = ConfigDict(from_attributes=True)

    national_id: str | None = None
    billing_entity_type: BillingEntity | None = None
    company_title: str | None = None
    tax_plate_name: str | None = None
    tax_office: str | None = None
    tax_number: str | None = None
    international_kind: InternationalKind | None = None
    contact_name: str | None = None


class AdminCustomerDetail(BaseModel):
    """One customer in full: who they are, who to invoice, where, what they have
    bought, and which price list they are on."""

    id: uuid.UUID
    email: str
    username: str | None
    full_name: str | None
    phone: str | None
    landline_phone: str | None
    account_type: AccountType
    is_active: bool
    is_verified: bool
    created_at: datetime
    segment: SegmentType | None
    order_count: int
    loyalty_balance: int
    profile: CustomerProfileRead | None
    addresses: list[CustomerAddressRead]
    # Their standing Kafe/Toptan/Bayi request, if they made one from their
    # account page. Decided from the applications queue, not here.
    application_status: str | None = None
    requested_segment: SegmentType | None = None


class SavedAddress(BaseModel):
    """A stored invoice address, offered at checkout as somewhere to ship to.

    Every field but the kind and the country is optional: it prefills the
    delivery form, which validates what is actually sent. An address on file is
    worth offering even when the account predates the field that would complete
    it.
    """

    #: The row's id; checkout does not need it, "Adreslerim" edits by it.
    id: int | None = None
    kind: AddressKind
    label: str
    full_name: str | None = None
    phone: str | None = None
    country: str
    city: str
    district: str | None = None
    postal_code: str | None = None
    line: str


class AddressBookEntry(SavedAddress):
    """One address in "Adreslerim", and what the customer may do with it.

    A saved delivery address is theirs to edit and delete. Their own invoice
    address can be corrected but not removed (an individual's; a firm's is the
    one it registered). The firm an individual is invoiced to is edited with
    the rest of its invoice details, at checkout.
    """

    editable: bool
    deletable: bool


class SegmentAssign(BaseModel):
    """Which price list this customer buys from, from now on."""

    segment: SegmentType
