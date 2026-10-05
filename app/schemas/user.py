import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import SegmentType, UserRole


class UserCreate(BaseModel):
    """Public sign-up payload. Segment is never client-controlled (always RETAIL)."""

    email: str
    password: str
    full_name: str | None = Field(default=None, max_length=150)
    phone: str | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    phone: str | None
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime


class UserMe(UserRead):
    """Authenticated user's own profile, including their pricing segment."""

    segment_code: SegmentType | None = None
    loyalty_balance: int = 0  # usable ParaPuan (1 point = 1 TRY)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: str
    password: str


class CompanyProfileCreate(BaseModel):
    company_title: str
    tax_number: str | None = None
    tax_office: str | None = None
    billing_address: dict | None = None
    delivery_address: dict | None = None


class CompanyProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_title: str
    tax_number: str | None
    tax_office: str | None
    billing_address: dict | None
    delivery_address: dict | None
