"""The panel team as the Sistem Yöneticisi manages it ("Yöneticiler")."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.security import ADMIN_PASSWORD_MIN
from app.schemas.registration import _EMAIL

USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{1,28}[A-Za-z0-9]$"
# The roles of the panel team; a customer is not one of them.
TeamRole = Literal["SUPER_ADMIN", "ADMIN", "STAFF"]


class TeamMemberRead(BaseModel):
    id: uuid.UUID
    email: str
    username: str | None
    full_name: str | None
    role: TeamRole
    is_active: bool
    is_superadmin: bool
    created_at: datetime
    # The caller's own row.
    is_me: bool


class TeamMemberCreate(BaseModel):
    email: str = Field(max_length=255)
    username: str | None = Field(default=None, min_length=3, max_length=30, pattern=USERNAME_PATTERN)
    full_name: str | None = Field(default=None, max_length=150)
    role: TeamRole = "ADMIN"
    password: str = Field(min_length=ADMIN_PASSWORD_MIN, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        found = value.strip().lower()
        if not _EMAIL.match(found):
            raise ValueError("Geçerli bir e-posta adresi girilmeli")
        return found

    @field_validator("full_name", "username", mode="before")
    @classmethod
    def _collapse(cls, value: str | None) -> str | None:
        return " ".join((value or "").split()) or None


class TeamMemberUpdate(BaseModel):
    """Only the fields given change."""

    full_name: str | None = Field(default=None, max_length=150)
    role: TeamRole | None = None
    is_active: bool | None = None


class TeamPasswordSet(BaseModel):
    password: str = Field(min_length=ADMIN_PASSWORD_MIN, max_length=128)
