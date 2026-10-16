"""Outgoing messages: every e-mail and SMS sent, password-reset tokens, and
the admin's bulk campaigns."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Channels and statuses, as stored.
EMAIL, SMS = "email", "sms"
PENDING, SENT, FAILED = "pending", "sent", "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationCampaign(Base):
    """A bulk e-mail or SMS from "İletişim & Bildirimler"."""

    __tablename__ = "notification_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    # Who it went to: an audience key ("all", "RETAIL", "CAFE"…) or "manual".
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))  # e-mail only
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # "text" or "html" (e-mail); SMS is always text.
    body_format: Mapped[str] = mapped_column(String(8), nullable=False, default="text")
    # SMS parts per message, as the gateway bills them.
    sms_segments: Mapped[int | None] = mapped_column(Integer)
    # queued → sending → done (or failed when nothing could be sent).
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationLog(Base):
    """One e-mail or SMS to one recipient, whatever sent it."""

    __tablename__ = "notification_logs"
    __table_args__ = (Index("ix_notification_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    # What it was for: "password_reset", "campaign"…
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_campaigns.id", ondelete="SET NULL"), index=True
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    # The driver that handled it: "smtp", "resend", "netgsm"…, or "mock".
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    # The gateway's own reference (job id, message id), when it gives one.
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """A "Şifremi unuttum" request: only a hash of the secret is kept.

    By e-mail the secret is a long random token in a link; by SMS a six-digit
    code, which is why wrong attempts are counted. Single use: `used_at` is set
    when it changes the password, and a new request voids the older ones.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (Index("ix_password_reset_tokens_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    # Not unique: the same six digits can come up twice for one user.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
