import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.schemas.catalog import IMAGE_URL_PATTERN

# A site path ("/catalog", "/#avantajlar") or an absolute https URL. Never
# "javascript:", "data:" or protocol-relative "//host".
_HREF = re.compile(r"^(?:/(?!/)[^\s\"'<>\\]*|https://[^\s\"'<>\\]+)$")

Requirement = Literal["flash_sales"]
TextAlign = Literal["left", "center", "right"]


def _photo(v: str | None, what: str) -> str | None:
    """A photo's address: a site path (/images/…) or an http(s) URL; blank is none."""
    v = _blank_none(v)
    if v is None:
        return None
    if v.startswith("/") and not v.startswith("//") and _HREF.match(v):
        return v
    if not re.match(IMAGE_URL_PATTERN, v):
        raise ValueError(f"{what} must be a site path (/images/…) or an http(s) URL")
    return v


class Point(BaseModel):
    """A part's centre on the slide, in % of its width (x) and height (y)."""

    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


#: The named sizes the first version of the editor saved, as the pixels they
#: stood for; a stored or sent name is read as its pixels.
NAMED_FONT_PX = {"sm": 14, "base": 16, "lg": 18, "xl": 20, "2xl": 24, "3xl": 30, "4xl": 36, "5xl": 48, "6xl": 60, "7xl": 72}

#: A slide text's size in px on the widest slider (≈952px); the storefront
#: scales it down with the slide. Set by dragging the text's corner in the editor.
FontPx = Annotated[int, Field(ge=8, le=160)]
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class HeroTextSizes(BaseModel):
    """A size per text; one left out keeps the slide's own."""

    model_config = {"extra": "forbid"}

    eyebrow: FontPx | None = None
    title: FontPx | None = None
    subtitle: FontPx | None = None

    @field_validator("eyebrow", "title", "subtitle", mode="before")
    @classmethod
    def _named(cls, v):
        return NAMED_FONT_PX.get(v, v) if isinstance(v, str) else v


class HeroTextColors(BaseModel):
    """A colour (#rrggbb) per text; one left out keeps the slide's own."""

    model_config = {"extra": "forbid"}

    eyebrow: HexColor | None = None
    title: HexColor | None = None
    subtitle: HexColor | None = None


class HeroBgPosition(BaseModel):
    """The photo's focal point (0–100 % across and down) and zoom (100 = fills the slide) on a phone."""

    model_config = {"extra": "forbid"}

    x: int = Field(ge=0, le=100)
    y: int = Field(ge=0, le=100)
    zoom: int = Field(default=100, ge=100, le=200)


class HeroLayout(BaseModel):
    """Where each of a slide's four parts sits; all four or none."""

    model_config = {"extra": "forbid"}

    eyebrow: Point
    title: Point
    subtitle: Point
    button: Point


class SocialWidget(BaseModel):
    """The split card's channels (left half) and its jump button's text (right
    half, which links to the card's own `href`, e.g. "/#avantajlar")."""

    model_config = {"extra": "forbid"}

    instagram_href: str | None = Field(default=None, max_length=300)
    instagram_label: str = Field(default="Instagram", max_length=40)
    youtube_href: str | None = Field(default=None, max_length=300)
    youtube_label: str = Field(default="YouTube", max_length=40)
    anchor_label: str = Field(default="Keşfet", max_length=40)

    @field_validator("instagram_href", "youtube_href")
    @classmethod
    def _links(cls, v: str | None) -> str | None:
        return _href(v)

    @field_validator("instagram_label", "youtube_label", "anchor_label")
    @classmethod
    def _label(cls, v: str) -> str:
        return _required(v)


def _href(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _HREF.match(value):
        raise ValueError("Link must be a site path starting with / or an https:// URL")
    return value


def _blank_none(value: str | None) -> str | None:
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _required(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Required")
    return value


# ── Hero slides ──────────────────────────────────────────────────────────────


class HeroSlideWrite(BaseModel):
    # Every text is optional: a banner with its words in the photo needs none,
    # and a slide with no text and no button is its image alone.
    eyebrow: str = Field(default="", max_length=60)
    # The title and subtitle may run over several lines ("\n").
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=240)
    # The button is optional: with no label there is none, and its link is not kept.
    cta_label: str = Field(default="", max_length=40)
    cta_href: str | None = Field(default="", max_length=300)
    guest_cta_label: str | None = Field(default=None, max_length=40)
    guest_cta_href: str | None = Field(default=None, max_length=300)
    image_url: str | None = Field(default=None, max_length=500)
    requires: Requirement | None = None
    text_align: TextAlign = "center"
    image_focus_x: int = Field(default=50, ge=0, le=100)
    image_focus_y: int = Field(default=50, ge=0, le=100)
    image_zoom: int = Field(default=100, ge=100, le=200)
    layout: HeroLayout | None = None
    # The dark wash over the photo (30%); off, the photo shows as it is.
    enable_overlay: bool = True
    font_sizes: HeroTextSizes | None = None
    text_colors: HeroTextColors | None = None
    # The phone's own design (below 768px), apart from the wide screen's: the
    # parts' positions on a 4:5 stage, the texts' sizes (px on a 375px-wide
    # stage) and the photo's crop. Null = as a phone has always shown it.
    mobile_layout: HeroLayout | None = None
    mobile_font_sizes: HeroTextSizes | None = None
    mobile_bg_position: HeroBgPosition | None = None
    # A portrait photo for phones in place of the wide one (an uploaded file's
    # address, an https URL, or a site path); none: the wide one, cropped.
    mobile_image: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @field_validator("eyebrow", "title", "subtitle", "cta_label", mode="before")
    @classmethod
    def _none_is_empty(cls, v: str | None) -> str:
        return "" if v is None else v

    @field_validator("title", "subtitle", mode="before")
    @classmethod
    def _lines(cls, v: str) -> str:
        # Line breaks kept, as "\n", with no blank space at either end of a line.
        if isinstance(v, str):
            v = "\n".join(line.strip() for line in v.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        return v

    @field_validator("eyebrow", "title", "subtitle", "cta_label")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("cta_href", "guest_cta_href")
    @classmethod
    def _links(cls, v: str | None) -> str | None:
        return _href(v)

    @field_validator("mobile_image", mode="before")
    @classmethod
    def _mobile_image(cls, v: str | None) -> str | None:
        return _photo(v, "The mobile image")

    @field_validator("guest_cta_label", "image_url", mode="before")
    @classmethod
    def _opt(cls, v: str | None) -> str | None:
        return _blank_none(v)

    @field_validator("image_url")
    @classmethod
    def _image(cls, v: str | None) -> str | None:
        if v is not None and not re.match(IMAGE_URL_PATTERN, v):
            raise ValueError("Image must be an http(s) URL without spaces, quotes or brackets")
        return v

    @model_validator(mode="after")
    def _guest_pair(self) -> "HeroSlideWrite":
        if self.cta_label and not self.cta_href:
            raise ValueError("The button needs a link")
        if not self.cta_label:
            self.cta_href = ""  # no button, no link
        if bool(self.guest_cta_label) != bool(self.guest_cta_href):
            raise ValueError("The guest button needs both a label and a link")
        return self


class HeroSlideRead(BaseModel):
    id: int
    eyebrow: str
    title: str
    subtitle: str
    cta_label: str
    cta_href: str
    guest_cta_label: str | None
    guest_cta_href: str | None
    # Resolved: the uploaded file's URL, else the external URL, else None.
    image: str | None
    image_url: str | None
    has_upload: bool
    requires: Requirement | None
    text_align: TextAlign
    image_focus_x: int
    image_focus_y: int
    image_zoom: int
    layout: HeroLayout | None
    enable_overlay: bool
    font_sizes: HeroTextSizes | None
    text_colors: HeroTextColors | None
    mobile_layout: HeroLayout | None
    mobile_font_sizes: HeroTextSizes | None
    mobile_bg_position: HeroBgPosition | None
    mobile_image: str | None
    is_active: bool
    sort_order: int


# ── Promo boxes ──────────────────────────────────────────────────────────────


class PromoBoxWrite(BaseModel):
    """The two cards beside the hero ("Promo Kutusu"): the community card on
    top (`text`, the channels in `widget`), the advantages card under it
    (`eyebrow`, `title`, `href`, the widget's link text), each on a photo."""

    eyebrow: str = Field(default="", max_length=60)
    title: str = Field(max_length=120)
    text: str | None = Field(default=None, max_length=240)
    href: str = Field(max_length=300)
    requires: Requirement | None = None
    widget: SocialWidget
    # Each card's photo, a site path or an http(s) URL; blank = the shop's own.
    top_card_image_url: str | None = Field(default=None, max_length=500)
    top_card_focus_x: int = Field(default=50, ge=0, le=100)
    bottom_card_image_url: str | None = Field(default=None, max_length=500)
    bottom_card_focus_x: int = Field(default=50, ge=0, le=100)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @field_validator("top_card_image_url", "bottom_card_image_url", mode="before")
    @classmethod
    def _photo(cls, v: str | None) -> str | None:
        return _photo(v, "A card's photo")

    @field_validator("eyebrow")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("title")
    @classmethod
    def _req(cls, v: str) -> str:
        return _required(v)

    @field_validator("href")
    @classmethod
    def _link(cls, v: str) -> str | None:
        return _href(v)

    @field_validator("text", mode="before")
    @classmethod
    def _opt(cls, v: str | None) -> str | None:
        return _blank_none(v)

    @model_validator(mode="after")
    def _link_required(self) -> "PromoBoxWrite":
        if not self.href:
            raise ValueError("Link is required")
        return self


class PromoBoxRead(BaseModel):
    id: int
    eyebrow: str
    title: str
    text: str | None
    href: str
    requires: Requirement | None
    widget: SocialWidget | None
    top_card_image_url: str | None = None
    top_card_focus_x: int = 50
    bottom_card_image_url: str | None = None
    bottom_card_focus_x: int = 50
    is_active: bool
    sort_order: int


class HomeContentRead(BaseModel):
    """Everything the homepage needs from the CMS, active items only, in order."""

    slides: list[HeroSlideRead]
    promos: list[PromoBoxRead]
