import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.user import SegmentType


class ApplicationStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class B2BApplication(Base):
    """A customer's request for Kafe / Toptan / Bayi pricing.

    The account stays RETAIL until an admin approves (and picks the segment):
    a segment is never self-assigned.
    """

    __tablename__ = "b2b_applications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_segment: Mapped[str] = mapped_column(Enum(SegmentType), nullable=False)
    company_title: Mapped[str] = mapped_column(String(255), nullable=False)
    tax_number: Mapped[str] = mapped_column(String(20), nullable=False)
    tax_office: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        Enum(ApplicationStatus), nullable=False, default=ApplicationStatus.PENDING, index=True
    )
    admin_note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
