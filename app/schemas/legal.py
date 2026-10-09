"""The shop's legal texts: what the storefront reads, and what an admin writes."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    # An empty body would leave checkout with a blank modal to accept.
    body: str = Field(min_length=1)

    @field_validator("title", "body", mode="before")
    @classmethod
    def _tidy(cls, value: str) -> str:
        """Trimmed before the length rules are applied, so a body of spaces is
        as empty as it looks."""
        return value.strip() if isinstance(value, str) else value
