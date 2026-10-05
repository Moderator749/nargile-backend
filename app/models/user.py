import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SegmentType(str, enum.Enum):
    RETAIL = "RETAIL"
    CAFE = "CAFE"
    WHOLESALE = "WHOLESALE"
    DEALER = "DEALER"


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    STAFF = "STAFF"
    CUSTOMER = "CUSTOMER"


class CustomerSegment(Base):
    __tablename__ = "customer_segments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        Enum(SegmentType), unique=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    users: Mapped[list["User"]] = relationship(back_populates="segment")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email", "email", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(150))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_segments.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(
        Enum(UserRole), default=UserRole.CUSTOMER, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    segment: Mapped[CustomerSegment | None] = relationship(back_populates="users")
    company_profile: Mapped["CompanyProfile | None"] = relationship(
        back_populates="user", uselist=False
    )


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    company_title: Mapped[str] = mapped_column(String(255), nullable=False)
    tax_number: Mapped[str | None] = mapped_column(String(20))
    tax_office: Mapped[str | None] = mapped_column(String(100))
    billing_address: Mapped[dict | None] = mapped_column(JSONB)
    delivery_address: Mapped[dict | None] = mapped_column(JSONB)

    user: Mapped[User] = relationship(back_populates="company_profile")
