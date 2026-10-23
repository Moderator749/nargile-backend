from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Brand(Base):
    """A brand for the "Markalar" menu and the homepage logo marquee.

    Products carry no reliable brand field yet, so a brand links to a catalog
    name search (`search_term`).
    """

    __tablename__ = "brands"
    __table_args__ = (Index("ix_brands_slug", "slug", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    search_term: Mapped[str] = mapped_column(String(100), nullable=False)
    # Path under MEDIA_ROOT (e.g. "brands/3f2a….png"); None = show the name as a wordmark.
    logo_path: Mapped[str | None] = mapped_column(String(255))
    # A line or two for the brand's own page (/marka/<slug>), above its products.
    description: Mapped[str | None] = mapped_column(Text)
    # Shown in the menu and the marquee.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
