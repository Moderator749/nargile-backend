import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.rich_text import RICH_TEXT_MAX, to_rich_html

from app.models.catalog import CurrencyType, WarehouseType
from app.models.user import SegmentType

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
    image_url: str | None = None
    # The page's introduction, as the admin wrote it; null = none of its own.
    description: str | None = None
    # Published products listed under it or any of its subcategories (each
    # once), and the newest `updated_at` among them: the sitemap lists a
    # category with products, dated by its newest one. Only the public list
    # (GET /categories/) fills them in.
    product_count: int = 0
    updated_at: datetime | None = None


class ProductCreate(BaseModel):
    base_code: str
    name: str
    brand: str | None = None
    # Rich text, cleaned like the product sheet's (app.core.rich_text).
    description: str | None = Field(default=None, max_length=RICH_TEXT_MAX)
    category_id: int | None = None
    tags: list[str] | None = None
    image_url: str | None = Field(default=None, max_length=500, pattern=IMAGE_URL_PATTERN)

    @field_validator("description")
    @classmethod
    def _clean_description(cls, text: str | None) -> str | None:
        return to_rich_html(text)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # The page's address, /product/<slug> (GET /products/<slug> reads it too).
    slug: str
    base_code: str
    name: str
    brand: str | None
    description: str | None
    # "Açıklama ve SEO": overrides for the product page's title and description;
    # null falls back to the name and the description.
    meta_title: str | None = None
    meta_description: str | None = None
    category_id: int | None
    tags: list | None
    # The cover; `image_urls` is the whole gallery, cover first.
    image_url: str | None = None
    image_urls: list[str] | None = None
    youtube_video_url: str | None = None
    # The colour labels, in the admin's order. Each one is a variant with its own
    # stock; `variants[].color_name` is what to add to the cart.
    color_names: list[str] | None = None
    # On the "Fırsat Ürünleri" list while this is true, whether or not the offer
    # is still claimable; `variants[].flash_sale` says whether it is.
    is_opportunity: bool = False
    # Badge colour for a product on offer ("Fırsat rengi"), "#rrggbb".
    opportunity_color: str | None = None
    # Last edit of the product itself — the sitemap's <lastmod>.
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _gallery_always_has_the_cover(self) -> "ProductRead":
        """A list, never null, and never empty while the product has a photo.

        Products written before galleries existed — or through the plain create
        endpoint — have only `image_url`.
        """
        if not self.image_urls and self.image_url:
            self.image_urls = [self.image_url]
        if self.color_names is None:
            self.color_names = []
        return self


class CatalogPrice(BaseModel):
    currency: CurrencyType
    price_excl_tax: Decimal
    price_incl_tax: Decimal
    vat_rate: Decimal


class FlashSaleInfo(BaseModel):
    """A claimable offer: checkout charges `price` until `ends_at`.

    `ends_at` is null for an offer with no deadline; it then runs until its deal
    stock runs out or the admin turns the opportunity off.
    """

    price: CatalogPrice
    ends_at: datetime | None = None


class CatalogVariantRead(BaseModel):
    """One buyable colour, priced for one segment only — never the full matrix.

    A product with colour pills has one of these per colour, in the admin's
    order, and the customer picks one on the product page; a colourless product
    has a single entry whose `color_name` is null. Retired colours are left out
    entirely.
    """

    id: uuid.UUID
    sku: str
    # The "Renk" this variant is; null on a product with no colour axis.
    color_name: str | None = None
    attributes: dict | None
    # Always TRY, because that is what the cart charges. None = not sold to this
    # segment (or its foreign price could not be converted today).
    price: CatalogPrice | None
    # Set only when the admin priced this segment in USD/EUR: the amount as they
    # typed it, to show beside the TRY figure. `price` is this, converted.
    origin_price: CatalogPrice | None = None
    list_price: CatalogPrice | None  # public RETAIL price, for "was/now" display
    # Live sale that beats `price` for this caller; None when there is none.
    flash_sale: FlashSaleInfo | None = None
    # Whether the caller can buy it: stock in their own list's quota
    # (stock_source); a B2B list never draws on the retail stock.
    in_stock: bool = True
    # How many of it, for a signed-in caller only — the most the product page
    # lets them order. A visitor gets the flag alone: quantities stay private
    # to those who can order, and checkout holds everyone to them anyway.
    available_quantity: int | None = None


class CatalogProductRead(ProductRead):
    variants: list[CatalogVariantRead] = []


class CatalogProductDetail(CatalogProductRead):
    """The product page: the product plus what is sold with it.

    Only the detail endpoint carries the cross-sells — a list page would repeat
    them for every row — and they are plain products, so the nesting stops here.
    """

    cross_sells: list[CatalogProductRead] = []
    # Every category the product is listed under, the primary one first: the
    # page's "İlgili Kategoriler" links.
    category_ids: list[int] = []


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


# ── B2B price catalogue (PDF) ────────────────────────────────────────────────


class PriceListRequest(BaseModel):
    """The products on the customer's screen, in screen order, and the price
    groups their catalogue shows."""

    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    groups: list[SegmentType] = Field(min_length=1, max_length=4)


class AdminCatalogRequest(BaseModel):
    """An admin's catalogue from the storefront ("PDF Oluştur"): the products of
    one section, in screen order, at one price group's prices — any group."""

    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    segment: SegmentType


class PriceListItem(BaseModel):
    product_id: uuid.UUID
    code: str
    name: str
    image_url: str | None
    # One entry per requested group the product is sold to, in TRY.
    prices: dict[SegmentType, CatalogPrice]
