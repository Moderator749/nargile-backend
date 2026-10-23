import re
import uuid

from pydantic import BaseModel, Field, field_validator

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
    # None = top-level menu item. Only two levels: a parent must itself be top-level.
    parent_id: int | None = None
    sort_order: int = Field(default=0, ge=0, le=100_000)
    # Off hides it (and its subcategories) from the storefront; products stay.
    is_active: bool = True
    # Cover photo for its showcase tile: an http(s) URL or a site path.
    image_url: str | None = Field(default=None, max_length=500)
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

    @field_validator("image_url")
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
    # Its place in the admin's order; None for one not ranked (it follows the ranked, by name).
    position: int | None


class CategoryProductOrderWrite(BaseModel):
    """The ranked products, first to last; the category's others follow by name."""

    product_ids: list[uuid.UUID] = Field(max_length=2000)
