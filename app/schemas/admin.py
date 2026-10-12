"""Pydantic schemas for admin-only operations."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.catalog import CurrencyType, WarehouseType
from app.models.order import OrderStatus, PaymentMethod, ShippingMethod, ShippingPayer, TransportMode
from app.models.user import SegmentType
from app.schemas.catalog import IMAGE_URL_PATTERN
from app.schemas.order import OrderItemRead


# ── Catalog management ───────────────────────────────────────────────────────

class AdminProductCreate(BaseModel):
    base_code: str
    name: str
    brand: str | None = None
    description: str | None = None
    category_id: int | None = None
    tags: list[str] | None = None
    image_url: str | None = Field(default=None, max_length=500, pattern=IMAGE_URL_PATTERN)


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
    price_excl_tax: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    price_incl_tax: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    vat_rate: Decimal = Field(ge=0, le=99.99, max_digits=4, decimal_places=2)

    @model_validator(mode="after")
    def _incl_not_below_excl(self) -> "SegmentPriceInput":
        if self.price_incl_tax < self.price_excl_tax:
            raise ValueError("price_incl_tax cannot be lower than price_excl_tax")
        return self


class PricingMatrixUpdate(BaseModel):
    """Defines prices for a variant across all segments in one request."""
    currency: CurrencyType = CurrencyType.TRY
    prices: list[SegmentPriceInput] = Field(min_length=1)


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
    # When the money was booked, for the order sheet's history table.
    paid_at: datetime | None = None


# ── Admin read models (panel views) ──────────────────────────────────────────

class SegmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: SegmentType
    label: str


class AdminVariantDetail(BaseModel):
    """One colour of a product, with its full price matrix and stock quotas."""

    id: uuid.UUID
    sku: str
    # The "Renk" this variant is; null on a product with no colour axis.
    color_name: str | None = None
    attributes: dict | None
    cost_price: Decimal | None
    # kg per unit (coal's 20 kg free-shipping rule); None = unknown.
    weight_kg: Decimal | None = None
    prices: list["AdminVariantPrice"]
    inventory: list[AdminInventoryRead]


class VariantWeightUpdate(BaseModel):
    """kg per unit; null clears it (coal then counts as 0 kg toward free shipping)."""

    weight_kg: Decimal | None = Field(default=None, gt=0, le=1000, max_digits=8, decimal_places=3)


class VariantWeightRead(BaseModel):
    id: uuid.UUID
    sku: str
    weight_kg: Decimal | None


class AdminVariantPrice(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment_id: int
    currency: CurrencyType
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class ProductSort(str, enum.Enum):
    """Sortable columns of the admin products table."""

    CODE = "code"
    NAME = "name"
    STOCK = "stock"
    PRICE = "price"


class SortDirection(str, enum.Enum):
    ASC = "asc"
    DESC = "desc"


class ProductIds(BaseModel):
    """The rows an admin ticked in the products table."""

    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class ProductDeleteResult(BaseModel):
    """What a bulk delete managed. A product that appears in an order is kept:
    removing it would take order lines — and the accounts — with it."""

    deleted: list[uuid.UUID] = []
    # id → why it was kept.
    refused: dict[uuid.UUID, str] = {}


class ProductPublishUpdate(BaseModel):
    """The row toggle in the admin products list."""

    is_published: bool


class ProductPublishRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_published: bool


class AdminProductDetail(BaseModel):
    id: uuid.UUID
    base_code: str
    name: str
    brand: str | None
    category_id: int | None
    tags: list | None
    image_url: str | None
    is_published: bool = True
    variants: list[AdminVariantDetail]


class AdminOrderItemDetail(OrderItemRead):
    """An order line with the catalogue data the admin order sheet prints.

    All four are optional: a variant (or its price row) may have been deleted
    since the order was placed, and the sheet must still show the line.
    """

    sku: str | None = None
    product_code: str | None = None
    product_name: str | None = None
    # The "Renk" that was sold; null on a colourless product.
    color_name: str | None = None
    vat_rate: Decimal | None = None


class AdminOrderListItem(BaseModel):
    """Order plus who placed it, for the admin order list and detail sheet."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_number: str
    user_id: uuid.UUID
    customer_email: str
    customer_name: str | None
    customer_phone: str | None = None
    # From the customer's company profile (B2B); null for retail buyers, who are
    # invoiced at the delivery address.
    company_title: str | None = None
    billing_address: dict | None = None
    total_amount: Decimal
    shipping_cost: Decimal
    discount_amount: Decimal
    payment_method: PaymentMethod
    status: OrderStatus
    tracking_code: str | None
    shipping_method: ShippingMethod = ShippingMethod.YURTICI_DOOR
    # Who pays the carrier; the order never includes carriage.
    shipping_payer: ShippingPayer = ShippingPayer.SHOP
    shipping_consent_at: datetime | None = None
    shipping_address: dict | None = None
    # "Başka biri teslim alacak": who signs for the parcel, when not the buyer.
    receiver_name: str | None = None
    receiver_phone: str | None = None
    # What the firm asked for: its Yurtiçi agreement, or the ambar to use.
    shipping_contract_no: str | None = None
    freight_company_name: str | None = None
    freight_phone: str | None = None
    # The branch this one goes to, when it is not the invoice address.
    shipping_branch_name: str | None = None
    # "Faturayı firmaya istiyorum": who to make this one out to.
    invoice_to_company: bool = False
    invoice_company_title: str | None = None
    invoice_trade_name: str | None = None
    invoice_tax_office: str | None = None
    invoice_tax_number: str | None = None
    # The firm's registered address, as this order recorded it.
    invoice_address: dict | None = None
    save_card: bool = False
    # "Sipariş Notu": the customer's message, for the sheet's own note block.
    order_note: str | None = None
    # An export order request: priced at export rates, paid for nothing, and
    # no stock taken. The panel shows it as a request until someone agrees a
    # freight price with the customer and turns it into an order.
    is_quote: bool = False
    transport_mode: TransportMode | None = None
    agreements_accepted_at: datetime | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    created_at: datetime
    items: list[AdminOrderItemDetail] = []
