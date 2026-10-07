"""Special-day announcement banner: public read, admin write."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.site import ANNOUNCEMENT_ID, SiteAnnouncement
from app.models.user import User
from app.schemas.site import AnnouncementRead, AnnouncementUpdate

router = APIRouter(tags=["announcements"])


@router.get("/banners/active", response_model=AnnouncementRead | None)
@router.get("/announcement", response_model=AnnouncementRead | None)
async def get_active_announcement(db: AsyncSession = Depends(get_db)):
    """The live banner, or null when none is active or it has expired."""
    row = await db.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    if row is None or not row.is_live(datetime.now(timezone.utc)):
        return None
    return row


@router.get("/admin/announcement", response_model=AnnouncementRead)
async def get_announcement_settings(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    return row or AnnouncementRead(message="", is_active=False)


@router.put("/admin/announcement", response_model=AnnouncementRead)
async def update_announcement(
    payload: AnnouncementUpdate,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    if row is None:
        row = SiteAnnouncement(id=ANNOUNCEMENT_ID)
        db.add(row)
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    row.updated_by = admin.id
    await db.commit()
    await db.refresh(row)
    return row
