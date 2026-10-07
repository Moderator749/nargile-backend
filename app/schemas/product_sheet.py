"""The admin's product sheet ("Ürün Ekle / Düzenle"), as one payload.

The sheet is a grid: product info, four stock quotas, a currency × segment price
matrix, the commercial rule flags and the "Fırsat" block. It maps onto several
tables (products, product_variants, inventory, variant_prices, flash_sales), so
it travels as one object and is written in one transaction — the admin fills one
form and presses save once.
"""

import re
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.catalog import CurrencyType
from app.models.user import SegmentType
from app.schemas.catalog import IMAGE_URL_PATTERN

_IMAGE_URL = re.compile(IMAGE_URL_PATTERN)
# youtu.be/ID, /watch?v=ID, /embed/ID, /shorts/ID — the id is what is stored.
_YOUTUBE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^&]*&)*v=|embed/|shorts/|live/)|youtu\.be/)[\w-]{6,}"
)

# Used when the admin types only one side of a price pair.
DEFAULT_VAT_RATE = Decimal("20.00")
# Sheet order: Bayi, Toptan, Kafe, Perakende.
SHEET_SEGMENTS = (SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE, SegmentType.RETAIL)


def vat_from_pair(excl: Decimal | None, incl: Decimal | None) -> Decimal:
    """The VAT rate the admin implied by typing both sides of a price."""
    if excl is None or incl is None or excl <= 0:
        return DEFAULT_VAT_RATE
    rate = ((incl / excl - 1) * 100).quantize(Decimal("0.01"))
    return rate if Decimal("0") <= rate <= Decimal("99.99") else DEFAULT_VAT_RATE


class SheetStock(BaseModel):
    """One quota per segment; the sheet's total is the sum, computed for display."""

    bayi: int = Field(default=0, ge=0)
    toptan: int = Field(default=0, ge=0)
    kafe: int = Field(default=0, ge=0)
    perakende: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.bayi + self.toptan + self.kafe + self.perakende


class SheetPriceCell(BaseModel):
    """One cell of the matrix. Empty (both sides None) means "not sold at this price"."""

    price_excl_tax: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    price_incl_tax: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    # The rate the form applied. Sent back on a read so the form can restore its
    # "KDV Oranı" select; when absent it is derived from the pair.
    vat_rate: Decimal | None = Field(default=None, ge=0, le=99.99, max_digits=4, decimal_places=2)

    @property
    def filled(self) -> bool:
        return (self.price_excl_tax or 0) > 0 or (self.price_incl_tax or 0) > 0

    def resolved(self) -> tuple[Decimal, Decimal, Decimal]:
        """(excl, incl, vat_rate), completing whichever side was left blank."""
        vat = self.vat_rate if self.vat_rate is not None else vat_from_pair(self.price_excl_tax, self.price_incl_tax)
        incl = self.price_incl_tax
        excl = self.price_excl_tax
        if incl is None or incl <= 0:
            incl = (excl * (1 + vat / 100)).quantize(Decimal("0.01"))  # type: ignore[union-attr]
        if excl is None or excl <= 0:
            excl = (incl / (1 + vat / 100)).quantize(Decimal("0.01"))
        return excl, incl, vat


class SheetPriceRow(BaseModel):
    """A currency row of the matrix: one cell per segment."""

    currency: CurrencyType
    cells: dict[SegmentType, SheetPriceCell] = Field(default_factory=dict)


class SheetRules(BaseModel):
    is_free_shipping: bool = False
    # "Adet şartı": free carriage only from this many units; null = no condition.
    free_shipping_min_quantity: int | None = Field(default=None, ge=1)
    has_havale_discount: bool = True
    # "Ciro indirimi": counts towards, and is discounted by, the volume tiers.
    has_volume_discount: bool = True
    gives_points: bool = True
    can_use_points: bool = True
    # "Sepet Kuralı": ağır kargo payı 3.000 TL üzeri sepetlerde %50'yi geçemez.
    is_cart_rule_applied: bool = False


class SheetOpportunity(BaseModel):
    """The "Fırsat" block. Its price drives the live flash sale while it runs."""

    is_opportunity: bool = False
    opportunity_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    opportunity_stock: int | None = Field(default=None, ge=0)
    opportunity_end_date: datetime | None = None
    opportunity_color: str | None = Field(default=None, max_length=20)
    # The same rules, as they apply while the deal runs.
    opp_is_free_shipping: bool = False
    opp_free_shipping_min_quantity: int | None = Field(default=None, ge=1)
    opp_has_havale_discount: bool = True
    opp_has_volume_discount: bool = True
    opp_gives_points: bool = True
    opp_can_use_points: bool = True

    @model_validator(mode="after")
    def _runnable(self) -> "SheetOpportunity":
        """An active opportunity needs a price; a deadline is optional — without
        one it runs until its stock runs out or the admin turns it off."""
        if self.is_opportunity and self.opportunity_price is None:
            raise ValueError("Fırsat fiyatı girilmeli")
        return self


MAX_CROSS_SELLS = 4


class CrossSellCard(BaseModel):
    """A pinned product as the form's preview grid shows it."""

    id: uuid.UUID
    base_code: str
    name: str
    image_url: str | None = None
    # The public (RETAIL) price as entered, so the admin sees what the card shows.
    price_incl_tax: Decimal | None = None
    currency: CurrencyType | None = None


class ProductSheetWrite(BaseModel):
    base_code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=2, max_length=255)
    brand: str | None = Field(default=None, max_length=100)
    category_id: int | None = None
    # "Renk": one or more labels, in the order the admin added them.
    color_names: list[str] = Field(default_factory=list, max_length=20)
    description: str | None = None
    # Gallery, in order; the first is the cover. The server keeps `image_url` in
    # step with it, so one-photo readers need no change.
    image_urls: list[str] = Field(default_factory=list, max_length=12)
    youtube_video_url: str | None = Field(default=None, max_length=500)
    # The sheet has no variant picker: one product, one variant, whose SKU
    # defaults to the product code.
    sku: str | None = Field(default=None, max_length=50)
    # Unit weight in kg, for the coal free-shipping rule; null = not recorded.
    # Leaving the field out of a save keeps whatever is stored (the product form
    # does not edit it); sending null clears it.
    weight_kg: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=3)
    # Off the storefront while false (the products list toggles this per row too).
    is_published: bool = True

    stock: SheetStock = SheetStock()
    prices: list[SheetPriceRow] = Field(default_factory=list)
    rules: SheetRules = SheetRules()
    opportunity: SheetOpportunity = SheetOpportunity()
    # "Birlikte satılanlar", in the order the admin arranged them.
    related_product_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_CROSS_SELLS)

    @field_validator("related_product_ids")
    @classmethod
    def _unique_related(cls, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for pid in ids:
            if pid not in seen:
                seen.append(pid)
        return seen


    @field_validator("image_urls", "color_names", mode="before")
    @classmethod
    def _tolerate_missing_lists(cls, value: object) -> object:
        """A product written before these columns existed has NULL in them, not an
        empty array — and the sheet has to be able to read (and copy) it."""
        return [] if value is None else value

    @field_validator("image_urls")
    @classmethod
    def _image_urls(cls, urls: list[str]) -> list[str]:
        """Absolute http(s) URLs, trimmed, blanks dropped, order kept."""
        cleaned = []
        for url in urls:
            url = url.strip()
            if not url:
                continue
            if len(url) > 500 or not _IMAGE_URL.match(url):
                raise ValueError(f"Geçersiz görsel adresi: {url[:60]}")
            cleaned.append(url)
        return cleaned

    @field_validator("color_names")
    @classmethod
    def _color_names(cls, names: list[str]) -> list[str]:
        """Trimmed, blank-free and de-duplicated, keeping the admin's order."""
        seen: list[str] = []
        for name in names:
            label = " ".join(name.split())[:40]
            if label and label.casefold() not in {s.casefold() for s in seen}:
                seen.append(label)
        return seen

    @field_validator("youtube_video_url")
    @classmethod
    def _youtube(cls, url: str | None) -> str | None:
        url = (url or "").strip()
        if not url:
            return None
        if not _YOUTUBE.match(url):
            raise ValueError("Geçerli bir YouTube bağlantısı girin")
        return url


class ProductSheetRead(ProductSheetWrite):
    """The saved sheet, as the form reloads it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    variant_id: uuid.UUID
    sku: str
    stock_total: int
    # The pinned products with enough to draw their cards, same order as the ids.
    related_products: list[CrossSellCard] = []
