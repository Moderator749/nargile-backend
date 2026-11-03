"""The panel team's channel: one shared stream of messages, and how far each
member has read it.

A message is plain text, as typed; links to orders and products are found in
it when it is shown, never stored as markup. A deleted message keeps its row
(who wrote what, and when it was withdrawn) but is never shown again.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdminMessage(Base):
    __tablename__ = "admin_messages"
    __table_args__ = (
        # How many a member has just sent (the posting limit).
        Index("ix_admin_messages_sender_created", "sender_id", "created_at"),
        # What was withdrawn lately, for the clients still showing it.
        Index("ix_admin_messages_deleted_at", "deleted_at"),
    )

    # Ever increasing: the clients' cursor ("everything after 1042").
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Who it was, as named then: kept after the account is gone.
    sender_name: Mapped[str] = mapped_column(String(150), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class AdminMessageRead(Base):
    """The last message a member has seen; everything after it is unread."""

    __tablename__ = "admin_message_reads"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    last_read_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
