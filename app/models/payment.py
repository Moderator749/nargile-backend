"""Card payment attempts made through PayTR.

One row per payment page opened (a token requested), so a customer who closes
the page and tries again leaves two rows and the order's history shows both.
PayTR identifies an attempt by `merchant_oid`, and its notification is matched
back to the order through this table.
"""

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"  # payment page opened, no notification yet
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="PAYTR")
    # PayTR's order reference: letters and digits only, unique per attempt.
    merchant_oid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # What was asked for, in TRY.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(Enum(PaymentStatus), nullable=False, default=PaymentStatus.PENDING)
    test_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # From the notification: what was actually collected (with any installment
    # interest), how, and why it failed.
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_type: Mapped[str | None] = mapped_column(String(20))
    failed_reason_code: Mapped[str | None] = mapped_column(String(20))
    failed_reason_msg: Mapped[str | None] = mapped_column(String(300))
    # The notification as PayTR posted it (minus nothing secret: it carries none).
    callback_payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
