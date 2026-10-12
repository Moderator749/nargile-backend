import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.catalog import IMAGE_URL_PATTERN

# A site path ("/catalog", "/#avantajlar") or an absolute https URL. Never
# "javascript:", "data:" or protocol-relative "//host".
_HREF = re.compile(r"^(?:/(?!/)[^\s\"'<>\\]*|https://[^\s\"'<>\\]+)$")

ADVANTAGE_ICONS = (
    "truck", "bike", "credit-card", "landmark", "piggy-bank", "trending-up",
    "badge-check", "clock", "gift", "percent", "shield", "package",
)

Requirement = Literal["flash_sales"]
TextAlign = Literal["left", "center", "right"]


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
    eyebrow: str = Field(default="", max_length=60)
    title: str = Field(max_length=120)
    subtitle: str = Field(default="", max_length=240)
    cta_label: str = Field(max_length=40)
    cta_href: str = Field(max_length=300)
    guest_cta_label: str | None = Field(default=None, max_length=40)
    guest_cta_href: str | None = Field(default=None, max_length=300)
    image_url: str | None = Field(default=None, max_length=500)
    requires: Requirement | None = None
    text_align: TextAlign = "center"
    image_focus_x: int = Field(default=50, ge=0, le=100)
    image_focus_y: int = Field(default=50, ge=0, le=100)
    image_zoom: int = Field(default=100, ge=100, le=200)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @field_validator("eyebrow", "subtitle")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("title", "cta_label")
    @classmethod
    def _req(cls, v: str) -> str:
        return _required(v)

    @field_validator("cta_href", "guest_cta_href")
    @classmethod
    def _links(cls, v: str | None) -> str | None:
        return _href(v)

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
        if not self.cta_href:
            raise ValueError("Button link is required")
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
    is_active: bool
    sort_order: int


# ── Promo boxes ──────────────────────────────────────────────────────────────


class PromoBoxWrite(BaseModel):
    row: Literal[1, 2] = 1
    variant: Literal["image", "text", "transparent"] = "text"
    eyebrow: str = Field(default="", max_length=60)
    title: str = Field(max_length=120)
    text: str | None = Field(default=None, max_length=240)
    href: str = Field(max_length=300)
    image_url: str | None = Field(default=None, max_length=500)
    badge: Literal["sok"] | None = None
    requires: Requirement | None = None
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

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

    @field_validator("text", "image_url", mode="before")
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
    def _link_required(self) -> "PromoBoxWrite":
        if not self.href:
            raise ValueError("Link is required")
        return self


class PromoBoxRead(BaseModel):
    id: int
    row: int
    variant: str
    eyebrow: str
    title: str
    text: str | None
    href: str
    image: str | None
    image_url: str | None
    has_upload: bool
    badge: str | None
    requires: Requirement | None
    is_active: bool
    sort_order: int


# ── Advantages ───────────────────────────────────────────────────────────────


class AdvantageWrite(BaseModel):
    icon: str = "badge-check"
    title: str = Field(max_length=80)
    highlight: str = Field(max_length=40)
    highlight_label: str = Field(default="", max_length=120)
    details: list[str] = Field(default_factory=list, max_length=6)
    footnote: str | None = Field(default=None, max_length=400)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @field_validator("title", "highlight")
    @classmethod
    def _req(cls, v: str) -> str:
        return _required(v)

    @field_validator("footnote", mode="before")
    @classmethod
    def _opt(cls, v: str | None) -> str | None:
        return _blank_none(v)

    @field_validator("icon")
    @classmethod
    def _known_icon(cls, v: str) -> str:
        if v not in ADVANTAGE_ICONS:
            raise ValueError(f"Icon must be one of: {', '.join(ADVANTAGE_ICONS)}")
        return v

    @field_validator("highlight_label")
    @classmethod
    def _strip_label(cls, v: str) -> str:
        return v.strip()

    @field_validator("details")
    @classmethod
    def _clean_details(cls, v: list[str]) -> list[str]:
        lines = [line.strip() for line in v if line.strip()]
        if any(len(line) > 200 for line in lines):
            raise ValueError("Each detail line is at most 200 characters")
        return lines


class AdvantageRead(BaseModel):
    id: int
    icon: str
    title: str
    highlight: str
    highlight_label: str
    details: list[str]
    footnote: str | None
    is_active: bool
    sort_order: int


class HomeContentRead(BaseModel):
    """Everything the homepage needs from the CMS, active items only, in order."""

    slides: list[HeroSlideRead]
    promos: list[PromoBoxRead]
    advantages: list[AdvantageRead]
