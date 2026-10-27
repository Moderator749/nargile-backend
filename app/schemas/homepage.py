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
from app.schemas.content import HeroTextColors, HeroTextSizes, Point, _href

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
    bento: bool = True  # "Görsel Vitrin": three photo boxes, over the deals
    bento2: bool = True  # "Görsel Vitrin 2": three more, between SultanPuan and Kumbara
    deals: bool = True  # "Fırsatları Kaçırma"
    bento3: bool = True  # "Görsel Vitrin 3": three more, under the deals (where the campaign boxes were)
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
    "perks", "brands_top", "picks", "lules", "sultanpuan", "bento2", "kumbara", "bento", "deals",
    "bento3", "promos", "brands", "brand_showcase", "kozlukler", "games", "showcase", "about",
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


# "Premium Ürün Yelpazesi" on a desktop: rows of four slots, two rows at most.
SHOWCASE_COLUMNS = 4
SHOWCASE_SLOTS = 2 * SHOWCASE_COLUMNS


class Showcase(_Strict):
    """The tiles fill the rows in order: one slot each, two for a wide one —
    one wide tile to a row, and only where its row has two slots left, so a row
    never has a hole. The same rules as the editor's (`lib/showcaseGrid.ts`)."""

    title: _text(80, required=True)
    subtitle: _text(240)
    tiles: list[ShowcaseTile] = Field(max_length=SHOWCASE_SLOTS)

    @model_validator(mode="after")
    def _fits_the_grid(self) -> "Showcase":
        row_used = slots = 0
        row_has_wide = False
        for number, tile in enumerate(self.tiles, start=1):
            if not tile.enabled:  # kept in the list, not drawn: no slot
                continue
            if tile.wide and (row_has_wide or row_used + 2 > SHOWCASE_COLUMNS):
                raise ValueError(f"Tile {number} is wide, but its row has no two slots left for it (one wide tile to a row)")
            span = 2 if tile.wide else 1
            slots += span
            row_used += span
            row_has_wide = row_has_wide or tile.wide
            if row_used == SHOWCASE_COLUMNS:
                row_used, row_has_wide = 0, False
        if slots > SHOWCASE_SLOTS:
            raise ValueError(f"The tiles take {slots} slots; two rows hold {SHOWCASE_SLOTS} (a wide tile takes two)")
        return self


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
    "bento2",  # "Görsel Vitrin 2", the second row of them
    "bento3",  # "Görsel Vitrin 3", the photo boxes under the deals
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


class BentoLayout(_Strict):
    """Where each text of a box sits: its centre, in % of the box; all three or none."""

    eyebrow: Point
    title: Point
    subtitle: Point


class BentoBox(_Strict):
    """One photo box of "Görsel Vitrin", with words on it as a hero slide has them.

    The box keeps one shape (3:4) at every width, so a text placed at x/y and
    sized in px of a 400px-wide box lands and scales the same on any screen.
    """

    image: RequiredImage
    href: Link
    # For screen readers and search engines; the visible words may not name the photo.
    alt: _text(120)
    # The photo's focal point, in % (0 = its left / top edge): dragged on the editor's preview.
    focus_x: int = Field(default=50, ge=0, le=100)
    focus_y: int = Field(default=50, ge=0, le=100)
    # Each optional; line breaks kept.
    eyebrow: _text(60) = ""
    title: _text(120) = ""
    subtitle: _text(240) = ""
    # None: the texts stacked at the bottom of the box.
    layout: BentoLayout | None = None
    font_sizes: HeroTextSizes | None = None
    text_colors: HeroTextColors | None = None
    # A dark wash rising from the bottom, so white words read over a light photo.
    overlay: bool = True
    overlay_strength: int = Field(default=55, ge=10, le=90)


class Bento(_Strict):
    """ "Görsel Vitrin": three photo boxes side by side, words on each."""

    boxes: list[BentoBox] = Field(min_length=3, max_length=3)

    @model_validator(mode="before")
    @classmethod
    def _from_tabs(cls, data: object) -> object:
        # The section's first version had two tabs of boxes without words: its
        # three-box row (or else the first tab's first three) carries over.
        if isinstance(data, dict) and "tabs" in data and "boxes" not in data:
            tabs = [t for t in data.get("tabs") or [] if isinstance(t, dict)]
            row = next((t for t in tabs if t.get("layout") == "row"), tabs[0] if tabs else {})
            data = {"boxes": [{k: v for k, v in tile.items() if k in ("image", "href", "alt", "focus_x")} for tile in (row.get("tiles") or [])[:3]]}
        return data


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


#: The campaign boxes (a big box and three small: "campaigns") gave way to
#: "Görsel Vitrin 3" in their place on 2026-10-27.
RETIRED_BLOCKS = {"campaigns": "bento3"}


def upgrade_document(data: dict) -> dict:
    """A document saved before a section was replaced, in today's terms: the
    retired section's place in the order, its switch and a phone's choice
    carry over to the one that replaced it (unless that has its own), and its
    content is dropped — the campaign boxes' words never had the photo a
    showcase box needs."""
    data = {k: v for k, v in data.items() if k not in RETIRED_BLOCKS}
    order = data.get("order")
    if isinstance(order, list):
        data["order"] = [
            RETIRED_BLOCKS.get(b, b) for b in order if not (b in RETIRED_BLOCKS and RETIRED_BLOCKS[b] in order)
        ]
    sections = data.get("sections")
    if isinstance(sections, dict):
        sections = dict(sections)
        for old, new in RETIRED_BLOCKS.items():
            if old in sections:
                value = sections.pop(old)
                sections.setdefault(new, value)
        data["sections"] = sections
    mobile = data.get("mobile")
    if isinstance(mobile, dict) and isinstance(mobile.get("hidden"), list):
        hidden = mobile["hidden"]
        data["mobile"] = {
            **mobile,
            "hidden": [
                RETIRED_BLOCKS.get(h, h) for h in hidden if not (h in RETIRED_BLOCKS and RETIRED_BLOCKS[h] in hidden)
            ],
        }
    return data


class HomepageDocument(_Strict):
    """What is stored: only the sections the admin has saved."""

    @model_validator(mode="before")
    @classmethod
    def _retired(cls, data: object) -> object:
        # "slider" (the buttons' size for every slide) moved onto each slide:
        # a document saved with it still reads.
        if isinstance(data, dict) and "slider" in data:
            data = {k: v for k, v in data.items() if k != "slider"}
        # A section since replaced (an admin tab opened before the change saves it too).
        return upgrade_document(data) if isinstance(data, dict) else data

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
    bento: Bento | None = None
    bento2: Bento | None = None  # "Görsel Vitrin 2": its own three boxes, edited as the first's
    bento3: Bento | None = None  # "Görsel Vitrin 3": the same, in the campaign boxes' place
    statement: Statement | None = None  # the "promos" section
    brand_showcase: BrandShowcaseCopy | None = None
    kozlukler: ProductRowCopy | None = None
    games: ProductRowCopy | None = None


class HomepageSettingsRead(BaseModel):
    data: HomepageDocument
    updated_at: datetime | None = None
