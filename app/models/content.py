"""Homepage content managed from the admin panel: hero slides, promo boxes and
the advantages ("Avantajlar") bento."""

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PromoBox(Base):
    __tablename__ = "promo_boxes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 1 = first promo grid (Module 6), 2 = second (Module 7). Up to 3 active boxes each.
    row: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "image" | "text" | "transparent"
    variant: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    eyebrow: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str | None] = mapped_column(String(240))
    href: Mapped[str] = mapped_column(String(300), nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(255))
    image_url: Mapped[str | None] = mapped_column(String(500))
    # "sok" = the minimal flash-sale pill.
    badge: Mapped[str | None] = mapped_column(String(20))
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
