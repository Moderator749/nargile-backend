"""The "Ana Sayfa Düzenleme" document: a public read for the storefront and an
admin read/replace for the panel."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.content import SiteContent
from app.models.user import User
from app.schemas.homepage import HomepageDocument, HomepageSettingsRead

router = APIRouter(tags=["home-settings"])

HOMEPAGE_KEY = "homepage"


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
