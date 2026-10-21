"""The "Ana Sayfa Düzenleme" document: a public read for the storefront and an
admin read/replace for the panel — and its English and Arabic: a translation
memory over the same texts."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.content import SiteContent
from app.models.user import User
from app.schemas.homepage import HomepageDocument, HomepageSettingsRead

router = APIRouter(tags=["home-settings"])

HOMEPAGE_KEY = "homepage"
TRANSLATIONS_KEY = "homepage_i18n"


async def _read(db: AsyncSession) -> HomepageSettingsRead:
    row = await db.get(SiteContent, HOMEPAGE_KEY)
    if row is None:
        return HomepageSettingsRead(data=HomepageDocument())
    # A document saved under an older shape must not take the homepage down:
    # whatever no longer validates is dropped and falls back to the default.
    data: dict = {}
    for key, value in (row.data or {}).items():
        try:
            data[key] = HomepageDocument.model_validate({key: value}).model_dump()[key]
        except ValueError:
            continue
    return HomepageSettingsRead(data=HomepageDocument.model_validate(data), updated_at=row.updated_at)


@router.get("/home/settings", response_model=HomepageSettingsRead, response_model_exclude_none=True)
async def get_homepage_settings(db: AsyncSession = Depends(get_db)):
    """Public: the storefront merges this over its own defaults."""
    return await _read(db)


@router.get("/admin/home/settings", response_model=HomepageSettingsRead, response_model_exclude_none=True)
async def admin_get_homepage_settings(
    _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    return await _read(db)


@router.put("/admin/home/settings", response_model=HomepageSettingsRead, response_model_exclude_none=True)
async def admin_put_homepage_settings(
    payload: HomepageDocument,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the document. Sections left out go back to the storefront default."""
    row = await db.get(SiteContent, HOMEPAGE_KEY)
    data = payload.model_dump(exclude_none=True, mode="json")
    if row is None:
        row = SiteContent(key=HOMEPAGE_KEY, data=data, updated_by=admin.id)
        db.add(row)
    else:
        row.data = data
        row.updated_by = admin.id
    await db.commit()
    await db.refresh(row)
    return await _read(db)


# ── Translations ──────────────────────────────────────────────────────────────
#
# The homepage's texts — the document above, the hero slides and campaign
# boxes, the defaults the storefront shows where nothing was saved — in
# English and Arabic, as a memory: each Turkish text, exactly as it appears,
# mapped to its translation. The storefront swaps every text it finds here
# and leaves the rest in Turkish, so reordering sections or slides never
# misaligns a translation, a text used twice is translated once, and a text
# the admin rewrites simply shows in Turkish until it is translated again.

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
TextMemory = Annotated[dict[_Text, Annotated[str, StringConstraints(strip_whitespace=True, max_length=6000)]], Field(max_length=2000)]


class HomeTranslations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    en: TextMemory = Field(default_factory=dict)
    ar: TextMemory = Field(default_factory=dict)

    @field_validator("en", "ar")
    @classmethod
    def _drop_blank(cls, memory: dict[str, str]) -> dict[str, str]:
        # A blank translation is no translation: the Turkish shows.
        return {source: text for source, text in memory.items() if text}


class HomeTranslationsRead(HomeTranslations):
    model_config = ConfigDict(extra="ignore")

    updated_at: datetime | None = None


async def _read_translations(db: AsyncSession) -> HomeTranslationsRead:
    row = await db.get(SiteContent, TRANSLATIONS_KEY)
    if row is None:
        return HomeTranslationsRead()
    try:
        return HomeTranslationsRead.model_validate({**(row.data or {}), "updated_at": row.updated_at})
    except ValueError:
        return HomeTranslationsRead(updated_at=row.updated_at)


@router.get("/home/translations", response_model=HomeTranslationsRead)
async def get_home_translations(db: AsyncSession = Depends(get_db)):
    """Public: the storefront applies the page's language to the homepage's texts."""
    return await _read_translations(db)


@router.put("/admin/home/translations", response_model=HomeTranslationsRead)
async def admin_put_home_translations(
    payload: HomeTranslations,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the memory (both languages)."""
    row = await db.get(SiteContent, TRANSLATIONS_KEY)
    data = payload.model_dump(mode="json")
    if row is None:
        db.add(SiteContent(key=TRANSLATIONS_KEY, data=data, updated_by=admin.id))
    else:
        row.data = data
        row.updated_by = admin.id
    await db.commit()
    return await _read_translations(db)
