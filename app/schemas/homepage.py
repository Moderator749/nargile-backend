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
    "shopping-cart", "trending-down",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Section switches ─────────────────────────────────────────────────────────


class Sections(_Strict):
    hero: bool = True  # hero slider and the category column beside it (always first)
    perks: bool = True  # the payment advantages bar
    brands_top: bool = True  # the brand ticker under the perks bar
    picks: bool = True  # "Sizin İçin Seçtiklerimiz"
    lules: bool = True  # "Lüle Alırken Kararsız Kalacaksınız"
    sultanpuan: bool = True  # the SultanPuan band
    kumbara: bool = True  # "Kumbara 20.000 TL"
    bento: bool = True  # "Görsel Vitrin": two tabs of photo boxes, over the deals
    deals: bool = True  # "Fırsatları Kaçırma"
    campaigns: bool = True  # the campaign boxes (the big one and the mini bento)
    promos: bool = True  # "Sektörde Referans Noktası" (the key is older than the section)
    brands: bool = True  # the brand ticker under the statement
    brand_showcase: bool = True  # "Markalarımız"
    kozlukler: bool = True  # "Közlükler": its category as a product row
    games: bool = True  # "Oyun & Eğlence": its category as a product row
    showcase: bool = True  # "Premium Ürün Yelpazesi" (the category bento)
    advantages: bool = True  # retired: the "Hacim büyür" grid is on no page now
    about: bool = True  # the homepage's closing passage ("Alt SEO Metni", seo_intro)
    footer: bool = True  # every page's footer (always last)

    @model_validator(mode="before")
    @classmethod
    def _top_ticker_follows_brands(cls, data: object) -> object:
        # "brands" once switched both brand tickers; a document saved before
        # the top one had its own switch keeps it as "brands" said.
        if isinstance(data, dict) and "brands_top" not in data and "brands" in data:
            data = {**data, "brands_top": data["brands"]}
        return data


#: The homepage's sections between the hero (always first) and the footer
#: (every page's, always last), in the order the storefront shows them unless
#: the admin has dragged them into another ("Bölümler"). Each is also a
#: switch in `Sections`.
HomeBlock = Literal[
    "perks", "brands_top", "picks", "lules", "sultanpuan", "kumbara", "bento", "deals",
    "campaigns", "promos", "brands", "brand_showcase", "kozlukler", "games", "showcase", "about",
]


def _unique_blocks(order: list[str]) -> list[str]:
    if len(order) != len(set(order)):
        raise ValueError("A section appears once in the order")
    return order


BlockOrder = Annotated[list[HomeBlock], Field(max_length=len(HomeBlock.__args__)), AfterValidator(_unique_blocks)]


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


class SeoIntro(_Strict):
    """"Alt SEO Metni": the passage that closes the homepage and every catalog
    listing above the footer. A field left empty falls back to the storefront's
    own copy for it. `content` is what "Devamını Oku" opens; a blank line in it
    starts a new paragraph."""

    title: _text(150)
    summary: _text(1500)
    content: _text(6000)


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


# ── Mobil Yönetim ────────────────────────────────────────────────────────────


def _required_image(value: str) -> str:
    found = _image(value)
    if found is None:
        raise ValueError("Image is required")
    return found


RequiredImage = Annotated[str, StringConstraints(max_length=500), AfterValidator(_required_image)]

#: Homepage sections a phone can go without. Each is hidden below the tablet
#: breakpoint only; a tablet and a desktop keep it.
MobileSection = Literal[
    "perks",  # the payment advantages bar under the hero
    "promos",  # the two boxes under the slider (channels, advantages)
    "brand_ticker",  # both brand strips
    "lules",  # "Lüle Alırken Kararsız Kalacaksınız"
    "sultanpuan",  # the SultanPuan band
    "kumbara",  # the Kumbara band
    "bento",  # "Görsel Vitrin", the photo boxes
    "campaigns",  # the campaign boxes under the deals
    "brand_statement",  # "Sektörde Referans Noktası"
    "brand_showcase",  # "Markalarımız"
    "kozlukler",  # the "Közlükler" row
    "games",  # the "Oyun & Eğlence" row
    "showcase",  # "Premium Ürün Yelpazesi"
]


class MobileHeroImage(_Strict):
    """A hero slide's portrait photo for phones (2:3 to 4:5), in place of its wide one."""

    slide_id: int = Field(ge=1)
    image: RequiredImage


class Mobile(_Strict):
    hero_images: list[MobileHeroImage] = Field(default_factory=list, max_length=30)
    hidden: list[MobileSection] = Field(default_factory=list, max_length=len(MobileSection.__args__))

    @model_validator(mode="after")
    def _unique(self) -> "Mobile":
        ids = [entry.slide_id for entry in self.hero_images]
        if len(ids) != len(set(ids)):
            raise ValueError("A slide has one mobile image")
        # The same switch twice is still one switch.
        self.hidden = list(dict.fromkeys(self.hidden))
        return self


# ── The sections' own copy ───────────────────────────────────────────────────
# Every section the homepage shows has its words (and, where it has any, its
# pictures and links) here. A text may hold a placeholder the storefront fills
# with a live figure from "Parametreler": {tutar} (an amount), {oran} (a rate),
# {bant} (the SultanPuan band).


class Perk(_Strict):
    """One item of the "Avantaj Çubuğu"; its figure, for the two discounts, is live."""

    icon: IconKey
    label: _text(40, required=True)
    note: _text(160)


class InstallmentsPerk(Perk):
    value: _text(60, required=True)


class Perks(_Strict):
    installments: InstallmentsPerk
    volume: Perk
    transfer: Perk


class Picks(_Strict):
    """ "Sizin İçin Seçtiklerimiz", its two tabs, and the free-shipping note beside it."""

    title: _text(80, required=True)
    subtitle: _text(200)
    bestsellers: _text(30, required=True)
    newest: _text(30, required=True)
    shipping_eyebrow: _text(60)
    shipping_text: _text(160)  # {tutar}: the free-shipping threshold


class Lules(_Strict):
    """ "Lüle Alırken Kararsız Kalacaksınız", and the courier note beside it."""

    title: _text(80, required=True)
    kinds: list[_text(40, required=True)] = Field(max_length=8)
    closing: _text(160)
    more: _text(40, required=True)
    href: Link
    courier_eyebrow: _text(60)
    courier_text: _text(120)


class SultanPuanCopy(_Strict):
    subtitle: _text(120)  # {bant}
    note: _text(200)
    call: _text(40)


class KumbaraCopy(_Strict):
    subtitle: _text(120)
    note: _text(200)  # {tutar}: the yearly target; {oran}: the bonus as a share of it
    call: _text(40)


class CampaignBox(_Strict):
    title: _text(80, required=True)
    subtitle: _text(160)
    href: Link
    # A photo behind the words; without one, the box keeps its gradient.
    image: Image = None


class CampaignLead(CampaignBox):
    eyebrow: _text(40)
    cta: _text(40, required=True)


class Campaigns(_Strict):
    """The big box and the mini bento: always three small boxes."""

    lead: CampaignLead
    minis: list[CampaignBox] = Field(min_length=3, max_length=3)


class BentoTile(_Strict):
    """One photo box: the picture, where it leads, and words for those who cannot see it."""

    image: RequiredImage
    href: Link
    # The box shows no words, so this is what a screen reader and a search engine get.
    alt: _text(120)
    # Where the crop centres across the photo (0 = its left edge): a wide photo
    # in a tall box keeps its subject.
    focus_x: int = Field(default=50, ge=0, le=100)


#: A tab's arrangement, and how many boxes it holds: "bento" is three tall
#: boxes side by side and two wide ones stacked beside them; "row" is three
#: 16:9 boxes in a row.
BENTO_LAYOUT_BOXES = {"bento": 5, "row": 3}


class BentoTab(_Strict):
    label: _text(30, required=True)
    layout: Literal["bento", "row"] = "bento"
    tiles: list[BentoTile] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def _boxes_fit_layout(self) -> "BentoTab":
        wanted = BENTO_LAYOUT_BOXES[self.layout]
        if len(self.tiles) != wanted:
            raise ValueError(f"A {self.layout} tab holds {wanted} boxes")
        return self


class Bento(_Strict):
    """ "Görsel Vitrin": two tabs of photo boxes, each in its own arrangement; the tab slides one in for the other."""

    tabs: list[BentoTab] = Field(min_length=2, max_length=2)


class Statement(_Strict):
    """ "Sektörde Referans Noktası": the heading, the line shown, the rest folded."""

    heading: _text(80, required=True)
    lead: _text(400)
    rest: _text(1000)


class BrandBlurb(_Strict):
    description: _text(400)
    # In place of the shop's own logo file.
    logo: Image = None


class BrandShowcaseCopy(_Strict):
    """ "Markalarımız": its heading, the button's words, and each house brand's line."""

    heading: _text(60, required=True)
    collection: _text(40, required=True)
    sultan: BrandBlurb
    oduman: BrandBlurb
    legend: BrandBlurb


class ProductRowCopy(_Strict):
    """A category's product row ("Közlükler", "Oyun & Eğlence")."""

    title: _text(80, required=True)
    subtitle: _text(200)
    more: _text(40, required=True)


# ── The document ─────────────────────────────────────────────────────────────


class HomepageDocument(_Strict):
    """What is stored: only the sections the admin has saved."""

    sections: Sections | None = None
    # The sections' order; one left out (added since it was saved) takes its
    # default place (see the storefront's lib/homeBlocks).
    order: BlockOrder | None = None
    navigation: Navigation | None = None
    deals: Deals | None = None
    brands: Brands | None = None
    showcase: Showcase | None = None
    advantages: Advantages | None = None
    seo_intro: SeoIntro | None = None
    footer: Footer | None = None
    mobile: Mobile | None = None
    perks: Perks | None = None
    picks: Picks | None = None
    lules: Lules | None = None
    sultanpuan: SultanPuanCopy | None = None
    kumbara: KumbaraCopy | None = None
    campaigns: Campaigns | None = None
    bento: Bento | None = None
    statement: Statement | None = None  # the "promos" section
    brand_showcase: BrandShowcaseCopy | None = None
    kozlukler: ProductRowCopy | None = None
    games: ProductRowCopy | None = None


class HomepageSettingsRead(BaseModel):
    data: HomepageDocument
    updated_at: datetime | None = None
