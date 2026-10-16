"""The shop's legal texts: what the storefront reads, and what an admin writes."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.rich_text import RICH_TEXT_MAX, to_rich_html
from app.models.legal import LegalKey


class LegalDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: LegalKey
    title: str
    body: str
    updated_at: datetime | None = None
    # Whether this is still the text the shop was set up with. The panel says so
    # rather than showing a date that looks like somebody's edit.
    edited: bool = False


class LegalDocumentUpdate(BaseModel):
    title: str = Field(min_length=2, max_length=150)
    # HTML from the panel's editor, cleaned on the way in. An empty body would
    # leave checkout with a blank modal to accept.
    body: str = Field(min_length=1, max_length=4 * RICH_TEXT_MAX)

    @field_validator("title", "body", mode="before")
    @classmethod
    def _tidy(cls, value: str) -> str:
        """Trimmed before the length rules are applied, so a body of spaces is
        as empty as it looks."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("body")
    @classmethod
    def _clean_body(cls, value: str) -> str:
        cleaned = to_rich_html(value)
        if not cleaned:
            raise ValueError("Metin boş olamaz")
        return cleaned
