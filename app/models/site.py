import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String
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
