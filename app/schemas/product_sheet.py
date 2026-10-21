"""The admin's product sheet ("Ürün Ekle / Düzenle"), as one payload.

The sheet is a grid: product info, the stock quotas, a currency × segment price
matrix, the commercial rule flags, the "Açıklama ve SEO" block and the "Fırsat"
block. It maps onto several tables (products, product_variants, inventory,
variant_prices, flash_sales), so it travels as one object and is written in one
transaction — the admin fills one form and presses save once.

Stock is per colour. `color_names` is the colour list as the admin arranged the
pills, and `variants` carries one block of four quotas for each of them; a
product with no colours has one unnamed ("Standart") variant instead. The two
are reconciled into a canonical `variants` list before the sheet is saved, so
the writer has one list to walk and cannot be handed a colour with no stock
block or a stock block for a colour that is gone.
"""

import re
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.rich_text import RICH_TEXT_MAX, to_rich_html
from app.core.slugs import PRODUCT_SLUG, PRODUCT_SLUG_MAX
from app.models.catalog import CurrencyType
from app.models.user import SegmentType
from app.schemas.catalog import IMAGE_URL_PATTERN
from app.schemas.i18n import ProductTranslations

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

    def __add__(self, other: "SheetStock") -> "SheetStock":
        return SheetStock(**{f: getattr(self, f) + getattr(other, f) for f in STOCK_FIELD_NAMES})


STOCK_FIELD_NAMES = ("bayi", "toptan", "kafe", "perakende")


def fold(label: str) -> str:
    """How two colour labels are compared: trimmed, collapsed, case-insensitive."""
    return " ".join(label.split()).casefold()


class SheetVariant(BaseModel):
    """One colour of the product, with the four stock quotas that colour sells from.

    `color` is None on the single "Standart" variant a colourless product has.
    `id` names a variant that already exists, so renaming a colour keeps its
    stock and its history instead of starting a new one; it is absent on a colour
    the admin has just added.
    """

    id: uuid.UUID | None = None
    color: str | None = Field(default=None, max_length=40)
    sku: str | None = Field(default=None, max_length=50)
    stock: SheetStock = SheetStock()

    @field_validator("color")
    @classmethod
    def _label(cls, color: str | None) -> str | None:
        label = " ".join((color or "").split())[:40]
        return label or None


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
    # "Bayide / Toptanda / Kafede Havale / EFT İndirimi": the same discount for
    # that B2B list too, on this product's lines.
    has_dealer_havale_discount: bool = False
    has_toptan_havale_discount: bool = False
    has_cafe_havale_discount: bool = False
    # "Ciro indirimi": counts towards, and is discounted by, the volume tiers.
    has_volume_discount: bool = True
    gives_points: bool = True
    can_use_points: bool = True
    # "Sepet Kuralı": counts as coal for the cargo rule (see Product.is_cart_rule_applied).
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
# Categories one product can be listed under.
MAX_CATEGORIES = 20


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
    # Every category the product is listed under, the primary one (its
    # breadcrumb's) first. `category_id` is that first one, kept in step by
    # `_primary_category`; a caller that sends only `category_id` (as the sheet
    # did before) gets just that one.
    category_ids: list[int] | None = Field(default=None, max_length=MAX_CATEGORIES)
    category_id: int | None = None
    # "Renk": one or more labels, in the order the admin added them. Each one is a
    # buyable colour with its own stock; see `variants`.
    color_names: list[str] = Field(default_factory=list, max_length=20)
    # ── "Açıklama ve SEO" ────────────────────────────────────────────────────
    # HTML from the panel's editor, cleaned on the way in (app.core.rich_text).
    description: str | None = Field(default=None, max_length=RICH_TEXT_MAX)
    # Override the product page's <title>; blank falls back to the product name.
    meta_title: str | None = Field(default=None, max_length=70)
    # Override its <meta name="description">; blank falls back to `description`.
    meta_description: str | None = Field(default=None, max_length=160)
    # The four fields above in English and Arabic ("Çeviriler"). Left out = the
    # stored ones stay (a caller that predates them must not wipe them).
    translations: ProductTranslations | None = None
    # The page's address, /product/<slug>. Blank keeps the current one (a new
    # product's comes from its name). Changing it leaves the old address
    # redirecting here, so links already out there keep working.
    slug: str | None = Field(default=None, max_length=PRODUCT_SLUG_MAX)
    # Gallery, in order; the first is the cover. The server keeps `image_url` in
    # step with it, so one-photo readers need no change.
    image_urls: list[str] = Field(default_factory=list, max_length=12)
    youtube_video_url: str | None = Field(default=None, max_length=500)
    # The stem every colour's stock code is derived from; defaults to the product
    # code, which is what the form sends (it has no separate box for this).
    sku: str | None = Field(default=None, max_length=50)
    # Unit weight in kg, for the coal free-shipping rule; null = not recorded.
    # Leaving the field out of a save keeps whatever is stored (the product form
    # does not edit it); sending null clears it.
    weight_kg: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=3)
    # Off the storefront while false (the products list toggles this per row too).
    is_published: bool = True

    # The four quotas as one block. On a colourless product it *is* the stock. On
    # a coloured one it is the total across the colours when read back, and the
    # opening stock of the first colour when a caller sends no `variants` at all.
    stock: SheetStock = SheetStock()
    # One block of quotas per colour, in `color_names` order. Reconciled against
    # `color_names` on the way in (see `_reconcile_variants`), so the saver always
    # gets exactly one entry per colour — or one unnamed entry when there are none.
    variants: list[SheetVariant] = Field(default_factory=list, max_length=20)
    prices: list[SheetPriceRow] = Field(default_factory=list)
    rules: SheetRules = SheetRules()
    opportunity: SheetOpportunity = SheetOpportunity()
    # "Birlikte satılanlar", in the order the admin arranged them.
    related_product_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_CROSS_SELLS)

    @model_validator(mode="after")
    def _primary_category(self) -> "ProductSheetWrite":
        ids = self.category_ids if self.category_ids is not None else [self.category_id] if self.category_id else []
        self.category_ids = list(dict.fromkeys(ids))  # de-duplicated, order kept
        self.category_id = self.category_ids[0] if self.category_ids else None
        return self

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

    @field_validator("slug")
    @classmethod
    def _clean_slug(cls, slug: str | None) -> str | None:
        slug = (slug or "").strip().lower()
        if not slug:
            return None
        if not PRODUCT_SLUG.match(slug):
            raise ValueError("Adres yalnızca küçük harf, rakam, tire ve nokta içerebilir (ör. sultan-nargile-takimi)")
        return slug

    @field_validator("description")
    @classmethod
    def _clean_description(cls, text: str | None) -> str | None:
        """Only the editor's safe tags are stored; plain text becomes paragraphs."""
        return to_rich_html(text)

    @field_validator("meta_title", "meta_description")
    @classmethod
    def _trim_to_none(cls, text: str | None) -> str | None:
        """An emptied textarea stores NULL, not "", so the fallbacks kick in."""
        return (text or "").strip() or None

    @model_validator(mode="after")
    def _reconcile_variants(self) -> "ProductSheetWrite":
        """Make `variants` say exactly one thing: the colours, with their stock.

        `color_names` decides which colours exist — it is what the admin's pills
        are — and `variants` only supplies each one's quotas (and the id that
        keeps them attached across a rename). A block for a colour that is gone is
        dropped; a colour with no block opens at zero.

        With no colours at all the product has one unnamed "Standart" variant,
        whose quotas are the sheet's own `stock`.

        A caller that sends no `variants` (every client before colours had stock,
        this sheet's own older payloads included) has its flat `stock` read as the
        first colour's: that is where a migrated product's stock sits, so a read →
        write round-trip of such a product does not move it.
        """
        if not self.color_names:
            standard = next((v for v in self.variants if v.color is None), None)
            self.variants = [
                SheetVariant(
                    id=standard.id if standard else None,
                    sku=standard.sku if standard else None,
                    color=None,
                    stock=self.stock if standard is None else standard.stock,
                )
            ]
            return self

        by_color = {fold(v.color): v for v in self.variants if v.color is not None}
        sent_nothing = not self.variants
        reconciled: list[SheetVariant] = []
        seen: set[uuid.UUID] = set()
        for position, label in enumerate(self.color_names):
            sent = by_color.get(fold(label))
            if sent is None and sent_nothing and position == 0:
                sent = SheetVariant(stock=self.stock)
            # One variant row cannot be two colours: a repeated id is a new colour.
            variant_id = sent.id if sent and sent.id not in seen else None
            if variant_id is not None:
                seen.add(variant_id)
            reconciled.append(
                SheetVariant(
                    id=variant_id,
                    sku=sent.sku if sent and variant_id is not None else None,
                    color=label,
                    stock=sent.stock if sent else SheetStock(),
                )
            )
        self.variants = reconciled
        return self


class ProductSheetRead(ProductSheetWrite):
    """The saved sheet, as the form reloads it.

    `variants` comes back with each colour's id and derived SKU, which is how a
    resave keeps a colour's stock attached to it. `stock` is the total across the
    colours, for the form's "Toplam Stok" box.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # The first colour's variant (the only one on a colourless product). Kept for
    # readers that only ever expected one; `variants` is the full list.
    variant_id: uuid.UUID
    # The stem, not a stock code: each colour's own code is in `variants[].sku`.
    # Reading back the stem is what lets a client send the sheet straight back
    # without the first colour's "-RENK" suffix growing a second one.
    sku: str
    stock_total: int
    # The pinned products with enough to draw their cards, same order as the ids.
    related_products: list[CrossSellCard] = []
