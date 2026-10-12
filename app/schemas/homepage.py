"""The "Ana Sayfa Düzenleme" document: every switch and every line of copy the
homepage, its menus and the footer read, as the admin last saved them.

Each top-level section is optional. A section the admin has never saved is
simply absent, and the storefront falls back to its own default for it, so the
shop looks exactly as it did until someone edits it. A section that *is*
present is complete: the panel always saves the whole section it shows.

Everything here is text the storefront prints or a link it follows, so the
limits are about keeping a slip of the keyboard from breaking a layout, and the
link rules are the same as the rest of the CMS: a site path or an https URL,
never "javascript:" or "data:".
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.catalog import IMAGE_URL_PATTERN
from app.schemas.content import _href

Text = Annotated[str, StringConstraints(strip_whitespace=True)]


def _text(max_length: int, required: bool = False):
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1 if required else 0, max_length=max_length)
    ]


def _link(value: str) -> str:
    found = _href(value)
    if found is None:
        raise ValueError("Link is required")
    return found


def _image(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.startswith("/") and not value.startswith("//") and _href(value):
        return value
    if re.match(IMAGE_URL_PATTERN, value):
        return value
    raise ValueError("Image must be a site path (/images/…) or an http(s) URL")


Link = Annotated[str, StringConstraints(max_length=300), AfterValidator(_link)]
OptionalLink = Annotated[str | None, Field(default=None, max_length=300), AfterValidator(lambda v: _href(v))]
Image = Annotated[str | None, Field(default=None, max_length=500), AfterValidator(_image)]
Slug = Annotated[str | None, Field(default=None, max_length=80, pattern=r"^[a-z0-9-]*$")]

#: Lucide icons the storefront maps these keys to; a tile or card shows its icon
#: while it has no photograph.
IconKey = Literal[
    "wind", "wrench", "flame", "layers", "sparkles", "shield", "package", "gamepad",
    "truck", "bike", "credit-card", "landmark", "trending-up", "badge-check", "headset", "store",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Section switches ─────────────────────────────────────────────────────────


class Sections(_Strict):
    hero: bool = True  # hero slider and the category column beside it
    promos: bool = True  # the promo boxes under the slider
    deals: bool = True  # "Fırsatları Kaçırma"
    brands: bool = True  # the brand ticker
    showcase: bool = True  # "Premium Ürün Yelpazesi"
    advantages: bool = True  # "Ayrıcalıklar & Avantajlar"
    about: bool = True  # the corporate close ("17+ Yıllık Tecrübe")
    footer: bool = True


# ── Menus ────────────────────────────────────────────────────────────────────


class NavItem(_Strict):
    """One menu entry. It points at a category, a brand, or a link of its own;
    with none of them it searches the catalog for its label."""

    # Blank on a category or brand entry = use that category's current name.
    label: _text(60)
    slug: Slug = None
    brand: Slug = None
    search: _text(80) | None = None
    href: OptionalLink = None
    accent: bool = False
    # Off = kept in the list (with its place and settings) but not shown.
    enabled: bool = True

    @model_validator(mode="after")
    def _named(self) -> "NavItem":
        if not self.label and not (self.slug or self.brand):
            raise ValueError("A menu entry needs a label unless it points at a category or brand")
        return self


class Navigation(_Strict):
    top: list[NavItem] = Field(max_length=12)
    sidebar: list[NavItem] = Field(max_length=30)

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_mode(cls, data: object) -> object:
        # The sidebar once had an automatic mode ("sidebar_mode"); it is always
        # the explicit list now. Documents saved with the key still load.
        if isinstance(data, dict) and "sidebar_mode" in data:
            data = {k: v for k, v in data.items() if k != "sidebar_mode"}
        return data


# ── Homepage sections ────────────────────────────────────────────────────────


class Deals(_Strict):
    title: _text(60, required=True)
    subtitle: _text(200)


class Brands(_Strict):
    items: list[_text(40, required=True)] = Field(max_length=40)


class ShowcaseTile(NavItem):
    eyebrow: _text(40)
    wide: bool = False
    icon: IconKey = "package"
    # Overrides the product photo the tile otherwise borrows from its category.
    image: Image = None


class Showcase(_Strict):
    title: _text(80, required=True)
    subtitle: _text(240)
    tiles: list[ShowcaseTile] = Field(max_length=24)


class AdvantageCard(_Strict):
    badge: _text(30)
    title: _text(80, required=True)
    description: _text(240)
    note: _text(200)
    image: Image = None
    icon: IconKey = "badge-check"


class Advantages(_Strict):
    eyebrow: _text(60)
    title: _text(80, required=True)
    subtitle: _text(240)
    cards: list[AdvantageCard] = Field(max_length=12)


class Stat(_Strict):
    value: _text(12, required=True)
    label: _text(40, required=True)


class About(_Strict):
    stats: list[Stat] = Field(max_length=6)
    text: _text(1500)
    badges: list[_text(40, required=True)] = Field(max_length=6)


# ── Footer ───────────────────────────────────────────────────────────────────


class Branch(_Strict):
    name: _text(40, required=True)
    address: _text(200, required=True)


class SocialLink(_Strict):
    label: _text(40, required=True)
    href: Link
    kind: Literal["youtube", "instagram", "facebook", "tiktok", "x", "link"] = "link"


class FooterLink(_Strict):
    label: _text(120, required=True)
    href: Link


class FooterColumn(_Strict):
    title: _text(40, required=True)
    links: list[FooterLink] = Field(max_length=10)
    # The pill under the list ("TÜM YAZILARI GÖR"); both or neither.
    cta_label: _text(40) = ""
    cta_href: OptionalLink = None

    @model_validator(mode="after")
    def _cta_pair(self) -> "FooterColumn":
        if bool(self.cta_label) != bool(self.cta_href):
            raise ValueError("The column button needs both a label and a link")
        return self


class Footer(_Strict):
    brand_name: _text(40, required=True)
    tagline: _text(60)
    phone: _text(30, required=True)
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    whatsapp_href: Link
    branches: list[Branch] = Field(max_length=6)
    hours: _text(120)
    social: list[SocialLink] = Field(max_length=8)
    blog: FooterColumn
    info: FooterColumn
    faq: FooterColumn
    copyright: _text(120)
    price_note: _text(200)
    warning: _text(200)


# ── The document ─────────────────────────────────────────────────────────────


class HomepageDocument(_Strict):
    """What is stored: only the sections the admin has saved."""

    sections: Sections | None = None
    navigation: Navigation | None = None
    deals: Deals | None = None
    brands: Brands | None = None
    showcase: Showcase | None = None
    advantages: Advantages | None = None
    about: About | None = None
    footer: Footer | None = None


class HomepageSettingsRead(BaseModel):
    data: HomepageDocument
    updated_at: datetime | None = None
