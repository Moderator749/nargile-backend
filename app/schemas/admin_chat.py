"""The panel team's channel ("Ekip Kanalı") and its daily note ("Günün Notu")."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

MESSAGE_MAX = 2000
NOTE_MAX = 500


class TeamMessageRead(BaseModel):
    id: int
    sender_id: uuid.UUID | None
    sender_name: str
    body: str
    created_at: datetime
    # The reader's own message.
    mine: bool
    # The reader may withdraw it: their own, or any for a super admin.
    can_delete: bool


class TeamMessagePage(BaseModel):
    messages: list[TeamMessageRead]
    # Withdrawn in the last few minutes: a client still showing them drops them.
    deleted_ids: list[int]
    # Older ones remain, for "load earlier" (a first page and `before_id` pages only).
    has_more: bool


class TeamMessageCreate(BaseModel):
    body: str = Field(max_length=MESSAGE_MAX)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Boş mesaj gönderilemez")
        return v


class UnreadRead(BaseModel):
    count: int
    # The newest message there is (0: none yet).
    latest_id: int
    # The reader has seen up to here.
    last_read_id: int


class MarkRead(BaseModel):
    last_read_id: int = Field(ge=0)


class DailyNoteRead(BaseModel):
    text: str | None
    updated_by_name: str | None
    updated_at: datetime | None


class DailyNoteWrite(BaseModel):
    # Empty clears it.
    text: str = Field(max_length=NOTE_MAX)

    @field_validator("text")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()
