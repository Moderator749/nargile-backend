import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.user import SegmentType

B2BSegment = Literal["CAFE", "WHOLESALE", "DEALER"]


class B2BApplicationCreate(BaseModel):
    """Optional part of sign-up: ask for Kafe / Toptan / Bayi pricing."""

    segment: B2BSegment
    company_title: str = Field(min_length=2, max_length=255)
    # VKN (10 digits) or TCKN (11 digits) for sole traders.
    tax_number: str = Field(max_length=20)
    tax_office: str | None = Field(default=None, max_length=100)

    @field_validator("company_title")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("tax_number")
    @classmethod
    def _tax_number(cls, v: str) -> str:
        digits = re.sub(r"\s+", "", v)
        if not re.fullmatch(r"\d{10,11}", digits):
            raise ValueError("Tax number must be 10 (VKN) or 11 (TCKN) digits")
        return digits

    @field_validator("tax_office", mode="before")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class B2BApplicationRead(BaseModel):
    id: int
    user_id: uuid.UUID
    email: str
    full_name: str | None
    requested_segment: SegmentType
    company_title: str
    tax_number: str
    tax_office: str | None
    status: str
    admin_note: str | None
    created_at: datetime
    decided_at: datetime | None


class B2BApprove(BaseModel):
    # Defaults to the requested segment; the admin may grant a different one.
    segment: B2BSegment | None = None


class B2BReject(BaseModel):
    note: str = Field(min_length=3, max_length=300)


# ── Dashboard ────────────────────────────────────────────────────────────────


class PeriodTotals(BaseModel):
    order_count: int
    order_total: Decimal
    card_total: Decimal
    transfer_total: Decimal


class DashboardRead(BaseModel):
    """Genel Bakış. Money figures are paid orders only (APPROVED/SHIPPED), by the
    date they were paid, in Turkey time; amounts are what the customer paid."""

    usd_try: Decimal | None  # TCMB; None when the feed is unavailable
    eur_try: Decimal | None
    rates_date: str | None
    member_count: int
    pending_applications: int
    daily: PeriodTotals  # today since 00:00
    weekly: PeriodTotals  # Monday 00:00 of this week
    monthly: PeriodTotals  # the 1st of this month
    yearly: PeriodTotals  # 1 January this year
