import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.countries import COUNTRY_NAMES, TURKEY

# The 81 provinces, as customers write them.
TR_PROVINCES = (
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Aksaray", "Amasya", "Ankara", "Antalya", "Ardahan",
    "Artvin", "Aydın", "Balıkesir", "Bartın", "Batman", "Bayburt", "Bilecik", "Bingöl", "Bitlis", "Bolu",
    "Burdur", "Bursa", "Çanakkale", "Çankırı", "Çorum", "Denizli", "Diyarbakır", "Düzce", "Edirne", "Elazığ",
    "Erzincan", "Erzurum", "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane", "Hakkari", "Hatay", "Iğdır",
    "Isparta", "İstanbul", "İzmir", "Kahramanmaraş", "Karabük", "Karaman", "Kars", "Kastamonu", "Kayseri",
    "Kilis", "Kırıkkale", "Kırklareli", "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya", "Manisa",
    "Mardin", "Mersin", "Muğla", "Muş", "Nevşehir", "Niğde", "Ordu", "Osmaniye", "Rize", "Sakarya", "Samsun",
    "Şanlıurfa", "Siirt", "Sinop", "Şırnak", "Sivas", "Tekirdağ", "Tokat", "Trabzon", "Tunceli", "Uşak",
    "Van", "Yalova", "Yozgat", "Zonguldak",
)
ISTANBUL = "İstanbul"

_TR_LOWER = str.maketrans("İIŞĞÜÖÇ", "iışğüöç")


def _key(name: str) -> str:
    return " ".join(name.translate(_TR_LOWER).lower().split())


def place_key(name: str) -> str:
    """How two place names are compared: trimmed, collapsed, Turkish-folded."""
    return _key(name)



_BY_KEY = {_key(p): p for p in TR_PROVINCES} | {"istanbul": ISTANBUL, "izmir": "İzmir"}
_PHONE = re.compile(r"^\+?[0-9 ()-]{10,20}$")
# Anywhere else in the world: a dialling code, any punctuation people use to
# group digits, and as few as seven of them (some countries really are that
# short). Nothing here pretends to know whether the number exists.
_INTL_PHONE = re.compile(r"^\+?[0-9 ()./-]{7,30}$")


def valid_phone(value: str) -> str:
    """A Turkish phone number as people write it, or ValueError."""
    found = (value or "").strip()
    if not _PHONE.match(found) or len(re.sub(r"\D", "", found)) < 10:
        raise ValueError("Geçerli bir telefon numarası girilmeli")
    return found


def valid_international_phone(value: str) -> str:
    """A phone number from any country, dialling code and all, or ValueError."""
    found = " ".join((value or "").split())
    if not _INTL_PHONE.match(found) or len(re.sub(r"\D", "", found)) < 7:
        raise ValueError("Enter a valid phone number, including the country code")
    return found


def valid_contact_phone(value: str) -> str:
    """A number somebody can be reached on, Turkish or not.

    A number written with its own dialling code is held to the international
    rule; anything else is read as Turkish, where ten digits is the floor. One
    field, two conventions, because a courier may have to call either.
    """
    found = (value or "").strip()
    if found.startswith("+") or found.replace(" ", "").startswith("00"):
        return valid_international_phone(found)
    return valid_phone(found)


def _trim(value: str | None) -> str | None:
    """Collapsed whitespace, or None for a field left empty."""
    return " ".join((value or "").split()) or None


class ShippingAddress(BaseModel):
    """Where one order goes, frozen onto the order itself.

    Turkish by default and held to the 81 provinces, because that is what a
    domestic carrier routes on. An address in another country keeps the same
    five fields and loosens two: its ``city`` is whatever subdivision that
    country uses, and it has no ilçe. The parts a Turkish address does not need
    — a postal code — are optional, and ``line`` carries the rest either way.
    """

    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(max_length=30)
    # ISO 3166-1 alpha-2. "TR" unless the order is an export.
    country: str = Field(default=TURKEY, min_length=2, max_length=2)
    city: str = Field(min_length=2, max_length=80)
    district: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    line: str = Field(min_length=5, max_length=300)

    @field_validator("full_name", "line")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("district", "postal_code", mode="before")
    @classmethod
    def _optional(cls, v: str | None) -> str | None:
        return _trim(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return valid_contact_phone(v)

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        code = v.strip().upper()
        if code not in COUNTRY_NAMES:
            raise ValueError("Unknown country")
        return code

    @model_validator(mode="after")
    def _by_country(self) -> "ShippingAddress":
        if self.country != TURKEY:
            return self
        try:
            self.city = _BY_KEY[_key(self.city)]
        except KeyError:
            raise ValueError("Unknown province") from None
        if not self.district or len(self.district) < 2:
            raise ValueError("İlçe girilmeli")
        return self


class AddressBookInput(BaseModel):
    """An address kept in "Adreslerim": where, without who.

    The place fields of :class:`ShippingAddress`, held to the same rules — a
    Turkish address to the 81 provinces and an ilçe — so whatever is saved here
    can be shipped to. The name and number are the order's, asked at checkout.
    """

    label: str | None = Field(default=None, max_length=60)
    country: str = Field(default=TURKEY, min_length=2, max_length=2)
    city: str = Field(min_length=2, max_length=80)
    district: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    line: str = Field(min_length=5, max_length=200)

    @field_validator("line")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("label", "district", "postal_code", mode="before")
    @classmethod
    def _optional(cls, v: str | None) -> str | None:
        return _trim(v)

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        code = v.strip().upper()
        if code not in COUNTRY_NAMES:
            raise ValueError("Unknown country")
        return code

    @model_validator(mode="after")
    def _by_country(self) -> "AddressBookInput":
        if self.country != TURKEY:
            self.district = None
            return self
        try:
            self.city = _BY_KEY[_key(self.city)]
        except KeyError:
            raise ValueError("Unknown province") from None
        if not self.district or len(self.district) < 2:
            raise ValueError("İlçe girilmeli")
        self.postal_code = None
        return self


class InvoiceAddress(BaseModel):
    """The official billing address of the firm an order is invoiced to.

    Only what an invoice has to print: the province, the ilçe and the rest of
    the address. There is no name or phone here — the firm's title is the name,
    and where a parcel goes is a separate question (``ShippingAddress``), even
    when the answer happens to be the same place.

    Turkish only, because a personal order invoiced to a firm is a Turkish tax
    document; a firm abroad buys as an export account.
    """

    city: str = Field(max_length=80)  # İl
    district: str = Field(max_length=80)  # İlçe
    # As long as the column that keeps it when the customer saves it to their
    # account (``customer_addresses.street``).
    line: str = Field(max_length=200)  # Açık adres

    @field_validator("city", "district", "line", mode="before")
    @classmethod
    def _collapse(cls, value: str) -> str:
        # Before the checks below, so whitespace alone cannot pass them.
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("city")
    @classmethod
    def _city(cls, value: str) -> str:
        try:
            return _BY_KEY[_key(value)]
        except KeyError:
            raise ValueError("Fatura adresindeki il 81 ilden biri olmalı") from None

    @model_validator(mode="after")
    def _complete(self) -> "InvoiceAddress":
        # Said in Turkish, like the cart that asks for it: these reach the
        # customer when something gets past the form.
        if len(self.district) < 2:
            raise ValueError("Fatura adresi için ilçe girin")
        if len(self.line) < 5:
            raise ValueError("Şirketin açık fatura adresini girin")
        return self


class StructuredAddress(BaseModel):
    """A Turkish address field by field, as the sign-up form asks for it.

    Only the district and the province are required: an invoice without them
    names no place, and neither does a shipment later. A street or a
    neighbourhood is required too — one of the two, not both, since villages
    have no street names and new districts have no neighbourhood worth writing.
    The rest (building, door) is detail a courier needs but the system cannot
    check.
    """

    # Fixed: an address with a country of its own is an InternationalAddress,
    # and this is also what tells the two apart when one field accepts either.
    country: Literal["TR"] = TURKEY
    neighbourhood: str | None = Field(default=None, max_length=120)  # Mahalle / Köy
    street: str | None = Field(default=None, max_length=120)  # Cadde / Sokak
    building_no: str | None = Field(default=None, max_length=20)  # Bina no
    building_name: str | None = Field(default=None, max_length=120)  # Bina adı
    door_no: str | None = Field(default=None, max_length=20)  # Kapı no
    district: str = Field(min_length=2, max_length=80)  # İlçe
    city: str = Field(max_length=40)  # Şehir

    @field_validator("neighbourhood", "street", "building_no", "building_name", "door_no", mode="before")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _trim(value)

    @field_validator("district")
    @classmethod
    def _district(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("city")
    @classmethod
    def _city(cls, value: str) -> str:
        try:
            return _BY_KEY[_key(value)]
        except KeyError:
            raise ValueError("Şehir 81 ilden biri olmalı") from None

    @model_validator(mode="after")
    def _addressable(self) -> "StructuredAddress":
        if not self.neighbourhood and not self.street:
            raise ValueError("Mahalle/köy veya cadde/sokak girilmeli")
        return self

    def single_line(self) -> str:
        return address_line(
            neighbourhood=self.neighbourhood,
            street=self.street,
            building_no=self.building_no,
            building_name=self.building_name,
            door_no=self.door_no,
        )


class InternationalAddress(BaseModel):
    """An address outside Turkey.

    Two lines, a city (or state, or province — one field, since which of them a
    country uses is its own business), an optional postal code and a country
    code. Nothing is validated against a national format: there are nearly two
    hundred of them, and a wrong guess would turn a customer away. The country
    is checked, because it decides the carrier, the duty and the paperwork.

    Its messages are in English, like the form it comes from.
    """

    country: str = Field(min_length=2, max_length=2)
    city: str = Field(min_length=2, max_length=80)  # city / state / province
    postal_code: str | None = Field(default=None, max_length=20)
    address_line1: str = Field(min_length=3, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)

    @field_validator("city", "address_line1")
    @classmethod
    def _collapse(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("postal_code", "address_line2", mode="before")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _trim(value)

    @field_validator("country")
    @classmethod
    def _country(cls, value: str) -> str:
        code = value.strip().upper()
        if code not in COUNTRY_NAMES:
            raise ValueError("Select a country")
        if code == TURKEY:
            raise ValueError("A Turkish address belongs to a Turkish account type")
        return code

    def single_line(self) -> str:
        return international_line(
            address_line1=self.address_line1,
            address_line2=self.address_line2,
            postal_code=self.postal_code,
        )


def address_line(
    *,
    neighbourhood: str | None = None,
    street: str | None = None,
    building_no: str | None = None,
    building_name: str | None = None,
    door_no: str | None = None,
) -> str:
    """A structured address as one line, the way it is written on a label.

    Checkout and the order sheet both speak :class:`ShippingAddress`, whose
    ``line`` is free text; this is how the parts become one line without losing
    any of them. Takes the parts rather than a model so a stored row can be
    printed without being re-validated.
    """
    parts = [
        neighbourhood,
        street,
        f"No: {building_no}" if building_no else None,
        building_name,
        f"Kapı No: {door_no}" if door_no else None,
    ]
    return ", ".join(p for p in parts if p)


def international_line(
    *,
    address_line1: str | None = None,
    address_line2: str | None = None,
    postal_code: str | None = None,
) -> str:
    """An international address as one line, the way it is written on a waybill.
    The city and the country stay separate: a carrier sorts by those."""
    return ", ".join(p for p in (address_line1, address_line2, postal_code) if p)
