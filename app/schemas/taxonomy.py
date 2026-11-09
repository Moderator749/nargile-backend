import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.rich_text import RICH_TEXT_MAX, to_rich_html
from app.core.slugs import slugify  # noqa: F401 (re-exported to the admin API)
from app.schemas.catalog import IMAGE_URL_PATTERN
from app.schemas.i18n import CategoryTranslations

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _valid_slug(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _SLUG.match(value):
        raise ValueError("Slug may contain lowercase letters, digits and single hyphens only")
    return value


# ── Categories ───────────────────────────────────────────────────────────────


class CategoryWrite(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    # Omitted/blank = derived from the name.
    slug: str | None = Field(default=None, max_length=180)
    # None = top-level menu item. Three levels at most (app.services.categories).
    parent_id: int | None = None
    sort_order: int = Field(default=0, ge=0, le=100_000)
    # Off hides it (and its subcategories) from the storefront; products stay.
    is_active: bool = True
    # Cover photo for its showcase tile: an http(s) URL or a site path.
    image_url: str | None = Field(default=None, max_length=500)
    # The category page's banner, the same kind of address; null removes it,
    # left out = the stored one stays.
    banner_url: str | None = Field(default=None, max_length=500)
    # The page's introduction, HTML from the panel's editor (cleaned here);
    # blank = the storefront's built-in copy.
    description: str | None = Field(default=None, max_length=RICH_TEXT_MAX)
    # The passage closing the page, in place of the shop's own "Alt SEO
    # Metni": a heading and HTML from the panel's editor (cleaned here).
    # Blank = the shop's; left out = the stored ones stay.
    seo_title: str | None = Field(default=None, max_length=160)
    seo_text: str | None = Field(default=None, max_length=RICH_TEXT_MAX)
    # The name and introduction in English and Arabic. Left out = the stored
    # ones stay.
    translations: CategoryTranslations | None = None

    @field_validator("description", "seo_text")
    @classmethod
    def _clean_description(cls, v: str | None) -> str | None:
        return to_rich_html(v)

    @field_validator("seo_title")
    @classmethod
    def _blank_title(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("image_url", "banner_url")
    @classmethod
    def _check_image(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if re.match(r"^/(?!/)[^\s\"'<>\\]+$", v) or re.match(IMAGE_URL_PATTERN, v):
            return v
        raise ValueError("Image must be a site path (/images/…) or an http(s) URL")

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str | None) -> str | None:
        return _valid_slug(v)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v


class AdminCategoryRead(BaseModel):
    id: int
    name: str
    slug: str
    parent_id: int | None
    sort_order: int
    is_active: bool
    image_url: str | None
    banner_url: str | None = None
    description: str | None = None
    seo_title: str | None = None
    seo_text: str | None = None
    translations: CategoryTranslations | None = None
    # Products listed directly under this category (not its subcategories),
    # whether it is their primary category or one more.
    product_count: int


class ProductCategoryAssign(BaseModel):
    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    # None = remove the category.
    category_id: int | None


class ProductCategoryAssignResult(BaseModel):
    updated: int


# ── One category's products, set at once ─────────────────────────────────────

# The "Genel Kurallar" switches of the product sheet (Product's columns).
BULK_RULE_SWITCHES = (
    "is_free_shipping",
    "has_havale_discount",
    "has_dealer_havale_discount",
    "has_toptan_havale_discount",
    "has_cafe_havale_discount",
    "has_volume_discount",
    "gives_points",
    "can_use_points",
    "is_cart_rule_applied",
)
# "KDV gösterimi" of "ERP ve Katalog Fiyatları", a column per customer group.
BULK_ERP_DISPLAY = ("erp_display_bayi", "erp_display_toptan", "erp_display_kafe", "erp_display_perakende")


class CategoryBulkSettings(BaseModel):
    """"Tüm Ürünlere Toplu Ayar Uygula": the product sheet's "Genel Kurallar" and
    the catalogue's "KDV gösterimi", written on every product of a category.

    Only the fields sent are written; one left out keeps each product's own
    value, so a single rule can be switched without resetting the rest. Sent as
    null, ``free_shipping_min_quantity`` drops the condition and an
    ``erp_display_*`` the group's choice (the catalogue's full split).
    """

    model_config = ConfigDict(extra="forbid")

    # The subcategories' products too, as the category's page lists them.
    include_subcategories: bool = True

    is_free_shipping: bool | None = None
    free_shipping_min_quantity: int | None = Field(default=None, ge=1, le=100000)
    has_havale_discount: bool | None = None
    has_dealer_havale_discount: bool | None = None
    has_toptan_havale_discount: bool | None = None
    has_cafe_havale_discount: bool | None = None
    has_volume_discount: bool | None = None
    gives_points: bool | None = None
    can_use_points: bool | None = None
    is_cart_rule_applied: bool | None = None

    erp_display_bayi: bool | None = None
    erp_display_toptan: bool | None = None
    erp_display_kafe: bool | None = None
    erp_display_perakende: bool | None = None

    @model_validator(mode="after")
    def _switches_are_yes_or_no(self) -> "CategoryBulkSettings":
        cleared = [f for f in BULK_RULE_SWITCHES if f in self.model_fields_set and getattr(self, f) is None]
        if cleared:
            raise ValueError(f"Bir kural Evet ya da Hayır olmalı: {', '.join(cleared)}")
        if not self.changes():
            raise ValueError("Uygulanacak bir ayar seçin")
        return self

    def changes(self) -> dict[str, bool | int | None]:
        """The product columns to write, and their values."""
        written = (*BULK_RULE_SWITCHES, "free_shipping_min_quantity", *BULK_ERP_DISPLAY)
        return {f: getattr(self, f) for f in written if f in self.model_fields_set}


class CategoryBulkScope(BaseModel):
    """How many products a bulk setting reaches: the category's own, and with
    its subcategories' (each product once)."""

    products: int
    with_subcategories: int


class CategoryBulkResult(BaseModel):
    updated: int


# ── Brands ───────────────────────────────────────────────────────────────────


class BrandWrite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str | None = Field(default=None, max_length=100)
    # Catalog search the brand links to; blank = the name.
    search_term: str | None = Field(default=None, max_length=100)
    # For the brand's page (/marka/<slug>), above its products; blank = none.
    description: str | None = Field(default=None, max_length=1000)
    # Only to take the logo off: null or "" clears it (the edit form's "Logoyu
    # kaldır", saved with the rest). Left out, or any other value, keeps it; a
    # logo is put on through POST /admin/brands/{id}/logo.
    logo_url: str | None = Field(default=None, max_length=2000)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @property
    def clears_logo(self) -> bool:
        return "logo_url" in self.model_fields_set and not (self.logo_url or "").strip()

    @field_validator("description")
    @classmethod
    def _blank_description(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str | None) -> str | None:
        return _valid_slug(v)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v


class BrandRead(BaseModel):
    id: int
    name: str
    slug: str
    search_term: str
    logo_url: str | None
    description: str | None = None
    is_active: bool
    sort_order: int


class BrandOrderWrite(BaseModel):
    """Every brand, in the order the menu and the marquee show them."""

    ids: list[int] = Field(min_length=1, max_length=500)


class CategoryProductOrderRow(BaseModel):
    """One product of a category page, as the order editor lists it."""

    product_id: uuid.UUID
    name: str
    base_code: str
    brand: str | None
    image_url: str | None
    is_published: bool
    # Its place in the admin's order (in its part: the top's, or the bottom's);
    # None for one not ranked (it follows the ranked, by name).
    position: int | None
    # Kept at the bottom ("En altta sabit"): after the unranked, products added later included.
    bottom: bool = False


class CategoryProductOrderWrite(BaseModel):
    """The ranked products, first to last; the category's others follow by
    name; then the ones kept at the bottom, first to last."""

    product_ids: list[uuid.UUID] = Field(max_length=2000)
    bottom_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
