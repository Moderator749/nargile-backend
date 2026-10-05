import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, ConfigDict

from app.models.user import SegmentType, UserRole


class UserCreate(BaseModel):
    email: str
    password: str
    phone: str | None = None
    segment_code: SegmentType | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    phone: str | None
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime


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
