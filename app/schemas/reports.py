"""Raporlar: sales and profitability, read-only.

Money is TRY. Revenue ("ciro") is what the goods sold for: each line after its
share of the order's discounts, VAT included, shipping left out. Profit and
margin are before VAT: the line's revenue without its VAT, less its cost.
A margin is worked out on the lines whose cost is known; how much of the
revenue that covers is reported beside it.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.models.order import PaymentMethod
from app.models.user import SegmentType


class Period(BaseModel):
    date_from: date
    date_to: date


class Kpis(BaseModel):
    revenue: Decimal
    orders: int
    average_order_value: Decimal
    revenue_excl_vat: Decimal
    # On the lines with a cost; None when no line has one.
    gross_profit: Decimal | None
    margin_percent: Decimal | None
    # How much of revenue_excl_vat had a cost to set against it.
    costed_share_percent: Decimal | None
    # Lines sold before costs were recorded, costed at today's cost.
    estimated_cost_lines: int
    # "Kurtarılan Ciro": paid orders that "Siparişi Kurtar" brought back.
    recovered_revenue: Decimal
    recovered_orders: int


class KpiChanges(BaseModel):
    """Against the previous period of the same length: percent, or None when
    there was nothing to compare with. The margin moves in points."""

    revenue: Decimal | None
    orders: Decimal | None
    average_order_value: Decimal | None
    gross_profit: Decimal | None
    recovered_revenue: Decimal | None
    margin_points: Decimal | None


class SummaryRead(BaseModel):
    period: Period
    previous_period: Period
    current: Kpis
    previous: Kpis
    changes: KpiChanges


Granularity = Literal["hour", "day", "week"]


class TrendPoint(BaseModel):
    # Where the bucket begins, in Turkey time.
    start: datetime
    revenue: Decimal
    orders: int
    average_order_value: Decimal


class TrendRead(BaseModel):
    granularity: Granularity
    points: list[TrendPoint]


class SegmentRow(BaseModel):
    segment: SegmentType
    label: str
    revenue: Decimal
    orders: int
    share_percent: Decimal
    gross_profit: Decimal | None
    margin_percent: Decimal | None


class TopRow(BaseModel):
    # The product's id, the category's or the brand's; "" for none.
    key: str
    name: str
    code: str | None = None
    quantity: int
    revenue: Decimal
    orders: int


class SlowMoverRow(BaseModel):
    product_id: uuid.UUID
    code: str
    name: str
    category: str | None
    brand: str | None
    is_published: bool
    stock: int
    # The stock at today's cost; None when the cost is unknown.
    stock_value: Decimal | None
    last_sold_at: datetime | None


class PaymentMethodRow(BaseModel):
    payment_method: PaymentMethod
    placed: int
    paid: int
    pending: int
    cancelled: int
    failed: int
    returned: int
    revenue: Decimal
    # Of the orders that are settled one way or the other (not still pending).
    conversion_percent: Decimal | None
    cancellation_percent: Decimal | None


class ProfitRow(BaseModel):
    key: str
    name: str
    code: str | None = None
    quantity: int
    revenue_excl_vat: Decimal
    cost: Decimal | None
    gross_profit: Decimal | None
    margin_percent: Decimal | None
    # Units sold with no cost known at all, left out of the margin.
    uncosted_quantity: int
    # Some of the cost is today's cost standing in for one never recorded.
    estimated: bool
    below_threshold: bool


class TierMargin(BaseModel):
    segment: SegmentType
    price_excl_vat: Decimal | None
    margin_percent: Decimal | None
    below_threshold: bool


class TierMarginRow(BaseModel):
    """A product's list prices against its cost, group by group, as of today."""

    product_id: uuid.UUID
    code: str
    name: str
    category: str | None
    brand: str | None
    cost: Decimal
    tiers: list[TierMargin]
    lowest_margin_percent: Decimal | None
    alert: bool


class MissingCostRow(BaseModel):
    product_id: uuid.UUID
    code: str
    name: str
    category: str | None
    brand: str | None
    is_published: bool
    stock: int
    # Sold in the last 90 days: which gaps matter most.
    sold_quantity_90d: int
    revenue_90d: Decimal
