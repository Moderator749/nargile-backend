from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.rich_text import html_to_text, to_rich_html
from app.models.note import NOTE_BODY_MAX, NOTE_TITLE_MAX


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str
    created_at: datetime
    updated_at: datetime
    # Whose note it is: the reader's own, or (only the Moderator sees others') its author.
    own: bool = True
    author: str | None = None
    # The lock, told only to who may turn it: the admin who locked it and the
    # Moderator. Anyone else reads null (and a delete is refused with a reason).
    locked: bool | None = None
    can_manage_lock: bool = False


class NoteLock(BaseModel):
    locked: bool


class NoteIds(BaseModel):
    note_ids: list[int] = Field(min_length=1, max_length=500)


class NoteDeleteResult(BaseModel):
    deleted: list[int] = []
    # id → why it was kept.
    refused: dict[int, str] = {}


class NoteWrite(BaseModel):
    title: str = Field(default="", max_length=NOTE_TITLE_MAX)
    body: str = Field(default="", max_length=NOTE_BODY_MAX)

    @model_validator(mode="after")
    def _not_empty(self) -> "NoteWrite":
        self.title = self.title.strip()
        # HTML from the panel's editor, cleaned like every rich-text field.
        self.body = to_rich_html(self.body) or ""
        if not self.title and not html_to_text(self.body):
            raise ValueError("A note needs a title or some text")
        return self
