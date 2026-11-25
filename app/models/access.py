"""Which customer groups may not see which parts of the shop ("Grup Erişim
Kısıtlamaları").

A rule hides one target from one viewer tier: a customer group's code
(RETAIL, CAFE, WHOLESALE, DEALER) or GUEST for a visitor who is not signed
in. A target is a category (with everything under it), a brand, or a
storefront page by its path. Nothing is hidden unless a rule says so; rules
are added and removed from the admin panel at any time and enforced by the
API (app.services.tier_access), not only by the pages.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AccessTier(str, enum.Enum):
    GUEST = "GUEST"  # not signed in
    RETAIL = "RETAIL"
    CAFE = "CAFE"
    WHOLESALE = "WHOLESALE"
    DEALER = "DEALER"


class AccessTarget(str, enum.Enum):
    CATEGORY = "CATEGORY"  # target: the category's id, as text; its subcategories go with it
    BRAND = "BRAND"  # target: the brand's id, as text
    PAGE_ROUTE = "PAGE_ROUTE"  # target: a storefront path without the language prefix ("/kampanyalar")


class TierAccessRule(Base):
    __tablename__ = "tier_access_rules"
    __table_args__ = (UniqueConstraint("tier", "target_type", "target", name="uq_tier_access_rule"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class CustomerAccessRule(Base):
    """A rule for one customer ("Cari bazında"): hides a target from that
    account on top of its group's rules. Gone with the account."""

    __tablename__ = "customer_access_rules"
    __table_args__ = (UniqueConstraint("user_id", "target_type", "target", name="uq_customer_access_rule"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
