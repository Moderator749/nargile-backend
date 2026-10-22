"""Homepage content managed from the admin panel: hero slides, promo boxes,
the advantages ("Avantajlar") bento, and the "Ana Sayfa Düzenleme" document."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HeroSlide(Base):
    __tablename__ = "hero_slides"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    eyebrow: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    cta_label: Mapped[str] = mapped_column(String(40), nullable=False)
    cta_href: Mapped[str] = mapped_column(String(300), nullable=False)
    # Optional second button, shown to guests only (e.g. "Bayi Girişi").
    guest_cta_label: Mapped[str | None] = mapped_column(String(40))
    guest_cta_href: Mapped[str | None] = mapped_column(String(300))
    # Background photo: an uploaded file (wins) or an external https URL.
    image_path: Mapped[str | None] = mapped_column(String(255))
    image_url: Mapped[str | None] = mapped_column(String(500))
    # "flash_sales": only shown while a flash sale is live.
    requires: Mapped[str | None] = mapped_column(String(20))
    # How the slide is laid out: the text block's alignment, and the photo's
    # focal point (0–100 % across and down) and zoom (100 = cover, up to 200).
    text_align: Mapped[str] = mapped_column(String(10), nullable=False, default="center", server_default="center")
    image_focus_x: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    image_focus_y: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    image_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    # Free positions for the four parts on a wide screen, as % of the slide:
    # {"eyebrow": {"x": 50, "y": 24}, "title": …, "subtitle": …, "button": …}.
    # Null = the default centred stack.
    layout: Mapped[dict | None] = mapped_column(JSONB)
    # The design: the dark wash over the photo (on by default), and a size and
    # a colour per text ({"eyebrow": "sm", "title": "6xl", …}; null = the slide's own).
    enable_overlay: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    font_sizes: Mapped[dict | None] = mapped_column(JSONB)
    text_colors: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PromoBox(Base):
    """The promo box beside the hero ("Sosyal & Avantaj"): a top band that jumps
    to `href` and a bottom band with the shop's channels (`widget`). The first
    active one whose requirement is met is shown."""

    __tablename__ = "promo_boxes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    eyebrow: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str | None] = mapped_column(String(240))
    href: Mapped[str] = mapped_column(String(300), nullable=False)
    # The channel links and the jump button's text (SocialWidget).
    widget: Mapped[dict | None] = mapped_column(JSONB)
    requires: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SiteContent(Base):
    """A whole page's editable copy and switches, as one validated JSON document.

    Keyed by page ("homepage"). The document holds only what the admin has
    saved: a section that is absent falls back to the storefront's own default,
    so the shop looks the same until someone edits it.
    """

    __tablename__ = "site_content"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
