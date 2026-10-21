"""The shop's own Turkish text in the storefront's other languages.

Turkish is the source and stays in the ordinary columns (`name`,
`description`, …); a product's or category's English and Arabic live beside it
in one `translations` JSON column: {"en": {...}, "ar": {...}}. A language that
is missing, or a field left blank, falls back to the Turkish on the storefront.

The fields carry the same limits as their Turkish columns, and translated
descriptions are cleaned by the same rich-text sanitizer.

A translation Claude wrote (app.services.auto_translate) is a `draft` until an
admin approves it: the storefront shows it, but the language is not counted
among the row's `locales`, so its page stays out of the index (noindex) and
out of the sitemap and hreflang until someone has read it.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.core.rich_text import RICH_TEXT_MAX, to_rich_html

#: The storefront's languages besides Turkish, the source.
TRANSLATED_LOCALES = ("en", "ar")
Locale = Literal["en", "ar"]

MetaTitle = Annotated[str, StringConstraints(strip_whitespace=True, max_length=70)]
MetaDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)]


class _Translation(BaseModel):
    #: Machine-written and not yet reviewed (shown, not indexed).
    draft: bool = False
    #: For a draft: a fingerprint of the Turkish it was written from, so an
    #: edit to the Turkish has it rewritten (app.services.auto_translate).
    source: Annotated[str, StringConstraints(max_length=32)] | None = None


class ProductTranslation(_Translation):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=RICH_TEXT_MAX)
    meta_title: MetaTitle | None = None
    meta_description: MetaDescription | None = None

    @field_validator("description")
    @classmethod
    def _clean_description(cls, text: str | None) -> str | None:
        return to_rich_html(text)

    @field_validator("meta_title", "meta_description")
    @classmethod
    def _blank_is_none(cls, text: str | None) -> str | None:
        return text or None


class CategoryTranslation(_Translation):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
    description: str | None = Field(default=None, max_length=RICH_TEXT_MAX)

    @field_validator("description")
    @classmethod
    def _clean_description(cls, text: str | None) -> str | None:
        return to_rich_html(text)


ProductTranslations = dict[Locale, ProductTranslation]
CategoryTranslations = dict[Locale, CategoryTranslation]


def stored(translations: dict[str, BaseModel] | None) -> dict | None:
    """The validated translations as the JSON column keeps them; None when none."""
    if not translations:
        return None
    # `draft` (and its `source`) only on a draft: a reviewed translation
    # stores just its text.
    return {
        locale: {k: v for k, v in t.model_dump(exclude_none=True).items() if t.draft or k not in ("draft", "source")}
        for locale, t in translations.items()
    }


def is_live(translation: dict | None) -> bool:
    """A translation the storefront may index: it has a name and is not a draft."""
    return bool(translation and translation.get("name") and not translation.get("draft"))
