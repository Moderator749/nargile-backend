import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.user import AccountType, SegmentType, UserRole


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
    loyalty_balance: int = 0  # usable ParaPuan (1 point = 1 TRY)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    """``email`` accepts either the e-mail address or the chosen username."""

    email: str
    password: str
