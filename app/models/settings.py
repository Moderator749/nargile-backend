"""Numbers the shop can change without a deploy.

One row per setting, keyed by name, the value kept as text so a table that
holds a threshold today can hold a flag or a label tomorrow without a
migration. Nothing reads these rows directly: ``app.services.shop_settings``
parses them and supplies the default when a row is missing or unreadable, so a
hand-edited value can never take the storefront down.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShopSetting(Base):
    """One configurable value, as last saved from the admin panel."""

    __tablename__ = "shop_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Text on purpose; the service that owns the key decides what it means
    # (a number, or a JSON list such as a discount ladder).
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # Null once the admin who changed it is deleted; the value stays.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
