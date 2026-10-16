import re
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.site import (
    BANNER_MESSAGE_MAX,
    BANNER_STYLE_PRESETS,
    DEFAULT_BANNER_ICON,
    DEFAULT_BANNER_STYLE,
    POPUP_MESSAGE_MAX,
    POPUP_TITLE_MAX,
)
from app.schemas.catalog import IMAGE_URL_PATTERN

_HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
# Characters IMAGE_URL_PATTERN rejects (unsafe inside CSS url()) but that real URLs contain.
_URL_UNSAFE = re.compile(r"[\s\"'()<>\\]")

# Mood icon beside the text: festive for bayrams, the flag for solemn/national days.
BannerIcon = Literal["celebration", "memorial", "campaign", "none"]


class AnnouncementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message: str
    is_active: bool
    expires_at: datetime | None = None
    show_countdown: bool = False
    background_style: str = DEFAULT_BANNER_STYLE
    bg_image_url: str | None = None
    icon_type: BannerIcon = DEFAULT_BANNER_ICON
    updated_at: datetime | None = None


class AnnouncementUpdate(BaseModel):
    message: str = Field(max_length=200)
    is_active: bool
    # Timezone required: a naive time would be read in the server's zone, not the admin's.
    expires_at: AwareDatetime | None = None
    show_countdown: bool = False
    background_style: str = DEFAULT_BANNER_STYLE
    bg_image_url: str | None = Field(default=None, max_length=500, pattern=IMAGE_URL_PATTERN)
    icon_type: BannerIcon = DEFAULT_BANNER_ICON

    @field_validator("background_style")
    @classmethod
    def _known_style(cls, value: str) -> str:
        if value in BANNER_STYLE_PRESETS:
            return value
        if _HEX_COLOUR.match(value):
            return value.lower()
        raise ValueError(f"Must be one of {', '.join(BANNER_STYLE_PRESETS)} or a #RRGGBB colour")

    @field_validator("bg_image_url", mode="before")
    @classmethod
    def _normalise_url(cls, value: str | None) -> str | None:
        """Blank becomes None; spaces, quotes and parentheses are percent-encoded, not rejected."""
        if isinstance(value, str):
            value = _URL_UNSAFE.sub(lambda m: f"%{ord(m.group()):02X}", value.strip())
        return value or None

    @model_validator(mode="after")
    def _consistent(self) -> "AnnouncementUpdate":
        self.message = self.message.strip()
        if self.is_active and not self.message:
            raise ValueError("An active announcement needs a message")
        if self.show_countdown and self.expires_at is None:
            raise ValueError("A countdown needs an expiry time")
        return self


class PopupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str = ""
    message: str
    is_active: bool
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    # The storefront remembers a dismissal by this, so an edited popup shows again.
    updated_at: datetime | None = None


class PopupUpdate(BaseModel):
    title: str = Field(default="", max_length=POPUP_TITLE_MAX)
    message: str = Field(max_length=POPUP_MESSAGE_MAX)
    is_active: bool
    # Timezone required, as for the banner's expiry.
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "PopupUpdate":
        self.title = self.title.strip()
        self.message = self.message.strip()
        if self.is_active and not self.message:
            raise ValueError("An active popup needs a message")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("The end must come after the start")
        return self


class BannerPresetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message: str
    icon_type: BannerIcon = DEFAULT_BANNER_ICON
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_scheduled: bool = False
    sort_order: int = 0
    updated_at: datetime | None = None


class BannerPresetWrite(BaseModel):
    message: str = Field(max_length=BANNER_MESSAGE_MAX)
    icon_type: BannerIcon = DEFAULT_BANNER_ICON
    # Timezone required, as for the banner's expiry.
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    is_scheduled: bool = False
    sort_order: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def _consistent(self) -> "BannerPresetWrite":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("A preset needs a text")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("The end must come after the start")
        if self.is_scheduled and not (self.starts_at and self.ends_at):
            raise ValueError("Automatic publishing needs a start and an end")
        return self
