import re
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models.user import SegmentType


class WalletLot(BaseModel):
    source: str | None
    amount: int
    remaining: int
    earned_at: datetime
    expires_at: datetime | None


class WalletEntry(BaseModel):
    id: uuid.UUID
    created_at: datetime
    # Signed: + earned / added, − spent / removed.
    points: int
    kind: str  # EARNED | SPENT | ADJUSTMENT | EXPIRED
    source: str | None
    note: str | None
    order_number: str | None
    expires_at: datetime | None
    # Spending on an order that was later cancelled: the points came back.
    void: bool = False


class KumbaraRead(BaseModel):
    accumulated: Decimal
    target: Decimal
    rate: Decimal


class WalletRead(BaseModel):
    balance: int
    expiring_soon: int
    lots: list[WalletLot]
    kumbara: KumbaraRead
    history: list[WalletEntry]


class PointAdjustment(BaseModel):
    """Admin correction: + adds points (expire in 1 year), − removes them."""

    amount: int = Field(ge=-1_000_000, le=1_000_000)
    note: str = Field(min_length=3, max_length=300)

    @field_validator("amount")
    @classmethod
    def _non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("Amount must not be zero")
        return v


class AdminCustomerRead(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    segment: SegmentType | None
    balance: int
    order_count: int


# ── Bank accounts ────────────────────────────────────────────────────────────

_IBAN_TR = re.compile(r"^TR\d{24}$")


def normalise_tr_iban(value: str) -> str:
    """Strip spaces, upper-case, and check the TR format and ISO 13616 checksum."""
    iban = re.sub(r"\s+", "", value).upper()
    if not _IBAN_TR.match(iban):
        raise ValueError("IBAN must be TR followed by 24 digits")
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    if int(digits) % 97 != 1:
        raise ValueError("IBAN checksum is invalid")
    return iban


class BankAccountWrite(BaseModel):
    bank_name: str = Field(min_length=2, max_length=100)
    account_holder: str = Field(min_length=2, max_length=150)
    iban: str = Field(max_length=42)
    branch: str | None = Field(default=None, max_length=100)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @field_validator("iban")
    @classmethod
    def _iban(cls, v: str) -> str:
        return normalise_tr_iban(v)

    @field_validator("bank_name", "account_holder")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("branch", mode="before")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class BankAccountRead(BaseModel):
    id: int
    bank_name: str
    account_holder: str
    iban: str
    branch: str | None
    is_active: bool
    sort_order: int
