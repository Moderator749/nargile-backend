import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.user import AccountType, SegmentType, UserRole
from app.schemas.address import valid_contact_phone


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    phone: str | None
    account_type: AccountType
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime


class UserMe(UserRead):
    """Authenticated user's own profile, including their pricing segment."""

    segment_code: SegmentType | None = None
    loyalty_balance: int = 0  # usable Sultan Puan (worth `conversion_rate` TRY each)
    # Campaign consent on file, and whether the one-time card should ask for it.
    marketing_email_consent: bool = False
    marketing_sms_consent: bool = False
    consent_prompt_due: bool = False


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    """``email`` accepts either the e-mail address or the chosen username."""

    email: str
    password: str


class ProfileUpdate(BaseModel):
    """"Hesap Bilgilerim": the name and the number an order is sent under."""

    full_name: str = Field(min_length=2, max_length=150)
    phone: str = Field(max_length=30)

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        name = " ".join(v.split())
        if len(name) < 2:
            raise ValueError("Ad soyad girilmeli")
        return name

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return valid_contact_phone(v)


class PasswordChange(BaseModel):
    """A new password, given with the current one."""

    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)
