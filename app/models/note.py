import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

NOTE_TITLE_MAX = 120
NOTE_BODY_MAX = 100_000  # HTML from the editor


class AdminNote(Base):
    """A note in the admin's "Not Defteri": personal, so only its author sees it
    (and the "Moderator" account, which sees every note's lock). It goes when
    the admin's account goes.

    A locked note can't be deleted, alone or ticked with others, until it is
    unlocked — by whoever locked it or by the Moderator."""

    __tablename__ = "admin_notes"
    __table_args__ = (Index("ix_admin_notes_user_updated", "user_id", "updated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(NOTE_TITLE_MAX), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # Who locked it; a locker whose account is gone leaves the lock to the Moderator.
    locked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
