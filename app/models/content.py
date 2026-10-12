"""Homepage content managed from the admin panel: hero slides, promo boxes,
the advantages ("Avantajlar") bento, and the "Ana Sayfa Düzenleme" document."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PromoBox(Base):
    __tablename__ = "promo_boxes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 1 = first promo grid (Module 6), 2 = second (Module 7). Up to 3 active boxes each.
    row: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "image" | "text" | "transparent" | "social" (the split social & advantages widget)
    variant: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    eyebrow: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str | None] = mapped_column(String(240))
    href: Mapped[str] = mapped_column(String(300), nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(255))
    image_url: Mapped[str | None] = mapped_column(String(500))
    # "sok" = the minimal flash-sale pill.
    badge: Mapped[str | None] = mapped_column(String(20))
    # Free positions for the card's parts on a wide screen, as % of the card:
    # eyebrow, title, text, button. Null = the default stacked card.
    layout: Mapped[dict | None] = mapped_column(JSONB)
    # The full-bleed photo: which way its dark veil runs ("to-t" = dark at the
    # foot, rising), and its focal point (0–100 %) and zoom (100 = cover).
    overlay_direction: Mapped[str] = mapped_column(String(10), nullable=False, default="to-t", server_default="to-t")
    image_focus_x: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    image_focus_y: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    image_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    # The "social" variant's channel links and button text; null otherwise.
    widget: Mapped[dict | None] = mapped_column(JSONB)
    requires: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Advantage(Base):
    """One "Avantajlar" tile, e.g. Kurye · "10:00 – 20:00" · "arası hızlı teslimat"."""

    __tablename__ = "advantages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Key into the frontend's icon set (see ADVANTAGE_ICONS in app.schemas.content).
    icon: Mapped[str] = mapped_column(String(30), nullable=False, default="badge-check")
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    highlight: Mapped[str] = mapped_column(String(40), nullable=False)
    highlight_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    details: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    footnote: Mapped[str | None] = mapped_column(Text)
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
