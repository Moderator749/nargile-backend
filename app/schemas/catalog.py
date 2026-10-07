import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog import CurrencyType, WarehouseType

# Absolute http(s) URL with no quotes, brackets or whitespace, so the frontend can
# safely drop it into an <img src> or a CSS url().
IMAGE_URL_PATTERN = r"^https?://[^\s\"'()<>\\]+$"


class CategoryCreate(BaseModel):
    name: str
    slug: str
    parent_id: int | None = None


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    parent_id: int | None
    sort_order: int = 0


class ProductCreate(BaseModel):
    base_code: str
    name: str
    brand: str | None = None
    description: str | None = None
    category_id: int | None = None
    tags: list[str] | None = None
    image_url: str | None = Field(default=None, max_length=500, pattern=IMAGE_URL_PATTERN)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    base_code: str
    name: str
    brand: str | None
    description: str | None
    category_id: int | None
    tags: list | None
    image_url: str | None = None


class CatalogPrice(BaseModel):
    currency: CurrencyType
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class FlashSaleInfo(BaseModel):
    """A live flash sale: checkout charges `price` until `ends_at`, then `price` of the variant."""

    price: CatalogPrice
    ends_at: datetime


class CatalogVariantRead(BaseModel):
    """A variant priced for one segment only — never the full price matrix."""

    id: uuid.UUID
    sku: str
    attributes: dict | None
    price: CatalogPrice | None  # caller's segment; None = not sold to this segment
    list_price: CatalogPrice | None  # public RETAIL price, for "was/now" display
    # Live sale that beats `price` for this caller; None when there is none.
    flash_sale: FlashSaleInfo | None = None
    # Stock in the caller's warehouse (a flag only: quantities stay private).
    in_stock: bool = True


class CatalogProductRead(ProductRead):
    variants: list[CatalogVariantRead] = []


class VariantCreate(BaseModel):
    sku: str
    barcode: str | None = None
    attributes: dict | None = None
    cost_price: Decimal | None = None


class VariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    barcode: str | None
    attributes: dict | None


class VariantPriceCreate(BaseModel):
    segment_id: int
    currency: CurrencyType = CurrencyType.TRY
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class VariantPriceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    variant_id: uuid.UUID
    segment_id: int
    currency: CurrencyType
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class InventoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    variant_id: uuid.UUID
    warehouse_type: WarehouseType
    quantity: int
