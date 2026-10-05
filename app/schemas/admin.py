"""Pydantic schemas for admin-only operations."""

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.catalog import CurrencyType, WarehouseType


# ── Catalog management ───────────────────────────────────────────────────────

class AdminProductCreate(BaseModel):
    base_code: str
    name: str
    brand: str | None = None
    description: str | None = None
    category_id: int | None = None
    tags: list[str] | None = None


class AdminVariantCreate(BaseModel):
    sku: str
    barcode: str | None = None
    attributes: dict | None = None
    cost_price: Decimal | None = None
    initial_retail_stock: int = 0
    initial_b2b_stock: int = 0


class AdminVariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    barcode: str | None
    attributes: dict | None
    cost_price: Decimal | None
    inventory: list["AdminInventoryRead"] = []


class AdminInventoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_type: WarehouseType
    quantity: int


# ── Pricing matrix ───────────────────────────────────────────────────────────

class SegmentPriceInput(BaseModel):
    """Price definition for a single segment."""
    segment_id: int
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class PricingMatrixUpdate(BaseModel):
    """Defines prices for a variant across all segments in one request."""
    currency: CurrencyType = CurrencyType.TRY
    prices: list[SegmentPriceInput]


class PricingMatrixRead(BaseModel):
    variant_id: uuid.UUID
    currency: CurrencyType
    prices: list["SegmentPriceRow"]


class SegmentPriceRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    segment_id: int
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


# ── Order management ─────────────────────────────────────────────────────────

class AdminOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_number: str
    user_id: uuid.UUID
    total_amount: Decimal
    shipping_cost: Decimal
    discount_amount: Decimal
    payment_method: str
    status: str
    tracking_code: str | None
    created_at: str
    updated_at: str


class OrderApproveResponse(BaseModel):
    order_id: uuid.UUID
    order_number: str
    new_status: str
    loyalty_points_awarded: int
    message: str
