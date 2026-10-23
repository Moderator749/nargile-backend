"""The shop's legal texts, editable from the admin panel.

One row per document, keyed by what it is rather than by a number, so checkout
can ask for the agreement it needs by name. The defaults live in
``app.core.legal_texts``; a missing row falls back to them.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LegalKey(str, enum.Enum):
    """Which text. PRIVACY_NOTICE is the short framed notice in the cart; the
    next three are the documents the footer and the checkout modals show, and
    COOKIES the footer's "Çerez Politikası"."""

    PRIVACY_NOTICE = "PRIVACY_NOTICE"
    PRIVACY = "PRIVACY"
    DISTANCE_SALES = "DISTANCE_SALES"
    TERMS = "TERMS"
    COOKIES = "COOKIES"


class LegalDocument(Base):
    __tablename__ = "legal_documents"

    # The key itself, as text: there is exactly one row per document.
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
