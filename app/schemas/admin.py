"""Pydantic schemas for admin-only operations."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog import CurrencyType, WarehouseType
from app.models.order import Carrier, OrderStatus, PaymentMethod, ShippingMethod, ShippingPayer, TransportMode
from app.models.user import SegmentType
from app.schemas.order import OrderItemRead


# ── Catalog management ───────────────────────────────────────────────────────


class AdminInventoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_type: WarehouseType
    quantity: int


# ── Pricing matrix ───────────────────────────────────────────────────────────


class SegmentPriceRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    segment_id: int
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


# ── Order management ─────────────────────────────────────────────────────────


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
    """One colour of a product, with its full price matrix and its stock."""

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


class PaymentFailureRead(BaseModel):
    """"Ödeme Hata Kaydı (PayTR)": why a PAYMENT_FAILED order's card was not
    charged, whose side it is on, and what to tell the customer
    (`services.payment_failures`)."""

    source: str  # CUSTOMER_BANK | SYSTEM_STORE
    advice: str
    code: str | None = None
    message: str | None = None
    at: datetime | None = None
    merchant_oid: str | None = None
    test_mode: bool = False


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
    # The shipment, once it has left ("Kargola"); see Order.
    carrier: Carrier | None = None
    carrier_name: str | None = None
    tracking_url: str | None = None
    shipped_at: datetime | None = None
    cargo_key: str | None = None
    carrier_status: str | None = None
    carrier_status_text: str | None = None
    carrier_synced_at: datetime | None = None
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
    recovered_at: datetime | None = None
    delivered_at: datetime | None = None
    returned_at: datetime | None = None
    return_reason: str | None = None
    # "Gizli Mağaza Notu": the shop's own note. Only here — OrderRead, which
    # every customer-facing answer is built from, never carries it.
    internal_note: str | None = None
    created_at: datetime
    items: list[AdminOrderItemDetail] = []
    # Only on a PAYMENT_FAILED order.
    payment_failure: PaymentFailureRead | None = None
