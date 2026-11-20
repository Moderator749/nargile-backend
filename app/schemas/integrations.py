"""What the /integrations routes answer (app.api.v1.integrations)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class RetailPrice(BaseModel):
    """A colour's Perakende price, in the currency it is kept in — the TRY row
    when there is one, as checkout charges it. A USD/EUR price is sold at the
    day's TCMB rate (`CatalogPage.rates`)."""

    currency: str
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class CatalogVariant(BaseModel):
    id: uuid.UUID
    sku: str
    color_name: str | None
    stock: int
    # Before VAT, in `cost_currency`; None = not known.
    cost_price: Decimal | None
    cost_currency: str
    retail: RetailPrice | None


class CatalogCategory(BaseModel):
    id: int
    name: str
    slug: str


class CetvelRecord(BaseModel):
    """The product's last applied "Fiyat Hesaplama Cetveli" row."""

    id: uuid.UUID
    applied_at: datetime
    updated_at: datetime


class SalesWindow(BaseModel):
    """Paid orders (approved, shipped, delivered; no quotes) in the window."""

    quantity: int
    retail_quantity: int
    # Line totals, VAT included, before order-level discounts.
    revenue: Decimal


class CatalogSales(BaseModel):
    d7: SalesWindow
    d30: SalesWindow
    d90: SalesWindow


class CatalogProduct(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    slug: str
    brand_id: int | None
    brand: str | None
    brand_slug: str | None
    category: CatalogCategory | None
    # Its primary category's names, from the top of the tree down.
    category_path: list[str]
    image_url: str | None
    is_published: bool
    # The first colour on sale's price, which stands for the product: prices
    # are product-wide in practice. `retail_uniform` says whether every active
    # colour has the same one.
    retail: RetailPrice | None
    retail_uniform: bool
    variants: list[CatalogVariant]
    cetvel: CetvelRecord | None
    sales: CatalogSales


class Rates(BaseModel):
    bulletin_date: date
    # TRY per unit (TCMB ForexSelling), as checkout uses.
    try_per_unit: dict[str, Decimal]


class HavaleTier(BaseModel):
    threshold: Decimal
    rate: Decimal


class CatalogPage(BaseModel):
    generated_at: datetime
    # None when TCMB is unreachable and no recent bulletin is kept.
    rates: Rates | None
    # The bank-transfer discount ladder: the worst a price can be cut at checkout.
    havale_tiers: list[HavaleTier]
    excluded_brand_slugs: list[str]
    products: list[CatalogProduct]
    # Pass as `after` for the next page; None on the last.
    next_after: uuid.UUID | None
