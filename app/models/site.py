import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ANNOUNCEMENT_ID = 1  # single site-wide announcement row

BANNER_STYLE_PRESETS = ("crimson_gradient", "dark_luxury", "festive_red")
DEFAULT_BANNER_STYLE = "crimson_gradient"

# Mood icon beside the text (allowed values: BannerIcon in app.schemas.site).
DEFAULT_BANNER_ICON = "celebration"


class SiteAnnouncement(Base):
    """Special-day banner shown at the top of the storefront (singleton, id=1)."""

    __tablename__ = "site_announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Past this instant the banner is hidden, whatever is_active says.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    show_countdown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # A preset name from BANNER_STYLE_PRESETS, or a custom "#RRGGBB" colour.
    background_style: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DEFAULT_BANNER_STYLE
    )
    bg_image_url: Mapped[str | None] = mapped_column(String(500))
    icon_type: Mapped[str] = mapped_column(String(16), nullable=False, default=DEFAULT_BANNER_ICON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def is_live(self, now: datetime) -> bool:
        """Active, has text, and not past its expiry."""
        if not self.is_active or not self.message:
            return False
        return self.expires_at is None or now < self.expires_at


POPUP_ID = 1  # single site-wide popup row
POPUP_TITLE_MAX = 80
POPUP_MESSAGE_MAX = 1000


class PopupAnnouncement(Base):
    """"Açılış duyurusu": the card shown once when a visitor lands on the
    storefront (singleton, id=1). A visitor who closes it does not see it again
    until it is changed."""

    __tablename__ = "popup_announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(POPUP_TITLE_MAX), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Shown from starts_at (null: at once) until ends_at (null: no end).
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def is_live(self, now: datetime) -> bool:
        """Active, has text, and within its time range."""
        if not self.is_active or not self.message:
            return False
        return (self.starts_at is None or self.starts_at <= now) and (self.ends_at is None or now < self.ends_at)


BANNER_MESSAGE_MAX = 200


class BannerPreset(Base):
    """A "Hazır metin" for the special-day banner, with its own date range.

    With `is_scheduled` on, the banner shows this text by itself from
    `starts_at` until `ends_at` (see app.api.v1.announcements), in the banner's
    own look, ahead of the text typed into the banner; afterwards the banner
    goes back to that text, or to the welcome strip.
    """

    __tablename__ = "banner_presets"
    # Created by the a8d2f6b31c74 migration; declared here so the model and the
    # database agree (`alembic check`).
    __table_args__ = (Index("ix_banner_presets_schedule", "is_scheduled", "starts_at", "ends_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message: Mapped[str] = mapped_column(String(BANNER_MESSAGE_MAX), nullable=False)
    icon_type: Mapped[str] = mapped_column(String(16), nullable=False, default=DEFAULT_BANNER_ICON)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "Otomatik yayınla": on, the range publishes and hides it by itself.
    is_scheduled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def is_live(self, now: datetime) -> bool:
        """Scheduled, and now is within its range."""
        return bool(
            self.is_scheduled and self.starts_at and self.ends_at and self.starts_at <= now < self.ends_at
        )
