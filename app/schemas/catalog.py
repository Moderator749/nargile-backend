import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.catalog import CurrencyType, WarehouseType


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


class ProductCreate(BaseModel):
    base_code: str
    name: str
    brand: str | None = None
    description: str | None = None
    category_id: int | None = None
    tags: list[str] | None = None


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    base_code: str
    name: str
    brand: str | None
    description: str | None
    category_id: int | None
    tags: list | None


class CatalogPrice(BaseModel):
    currency: CurrencyType
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class CatalogVariantRead(BaseModel):
    """A variant priced for one segment only — never the full price matrix."""

    id: uuid.UUID
    sku: str
    attributes: dict | None
    price: CatalogPrice | None  # caller's segment; None = not sold to this segment
    list_price: CatalogPrice | None  # public RETAIL price, for "was/now" display


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
