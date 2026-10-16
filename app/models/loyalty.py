import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TransactionType(str, enum.Enum):
    EARNED = "EARNED"
    SPENT = "SPENT"
    EXPIRED = "EXPIRED"
    ADJUSTMENT = "ADJUSTMENT"


class PointSource(str, enum.Enum):
    # Names kept from before the "Sultan Puan" rename, so old rows read the same.
    PARAPUAN = "PARAPUAN"  # standard Sultan Puan on a single paid order inside the band
    KUMBARA = "KUMBARA"  # annual Sultan Puan bonus (formerly Kumbarapuan)
    CHECKOUT = "CHECKOUT"  # redeemed in the cart
    ADMIN = "ADMIN"  # manual adjustment by an admin


class LoyaltyLedger(Base):
    """Append-only point ledger. Balance = unexpired earned lots minus spending,
    consumed first-expiring-first (see app.services.wallet). A SPENT row whose
    order was cancelled no longer counts, so the points return automatically."""

    __tablename__ = "loyalty_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id")
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[str] = mapped_column(
        Enum(TransactionType), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(Enum(PointSource))
    note: Mapped[str | None] = mapped_column(String(300))
    # Admin who made a manual adjustment.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
