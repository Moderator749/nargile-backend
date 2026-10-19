"""Announcements: the special-day banner and the landing popup. Public read, admin write."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.site import ANNOUNCEMENT_ID, POPUP_ID, BannerPreset, PopupAnnouncement, SiteAnnouncement
from app.models.user import User
from app.schemas.site import (
    AnnouncementRead,
    AnnouncementUpdate,
    BannerPresetRead,
    BannerPresetWrite,
    PopupRead,
    PopupUpdate,
)

router = APIRouter(tags=["announcements"])


@router.get("/banners/active", response_model=AnnouncementRead | None)
async def get_active_announcement(db: AsyncSession = Depends(get_db)):
    """The live banner, or null when none is active or it has expired.

    A scheduled "Hazır metin" whose range covers now comes first — the latest
    to start, when two overlap — in the banner's look, counting down to nothing
    and hiding at its end. Otherwise the banner as typed, while it is live.
    """
    now = datetime.now(timezone.utc)
    row = await db.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    preset = await db.scalar(
        select(BannerPreset)
        .where(
            BannerPreset.is_scheduled.is_(True),
            BannerPreset.starts_at <= now,
            BannerPreset.ends_at > now,
        )
        .order_by(BannerPreset.starts_at.desc(), BannerPreset.id.desc())
        .limit(1)
    )
    if preset is not None:
        look = AnnouncementRead.model_validate(row) if row else AnnouncementRead(message="", is_active=False)
        return look.model_copy(
            update={
                "message": preset.message,
                "is_active": True,
                "expires_at": preset.ends_at,
                "show_countdown": False,
                "icon_type": preset.icon_type,
                "updated_at": preset.updated_at,
            }
        )
    if row is None or not row.is_live(now):
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


# ── Landing popup ("Açılış duyurusu") ────────────────────────────────────────


@router.get("/announcements/popup", response_model=PopupRead | None)
async def get_live_popup(db: AsyncSession = Depends(get_db)):
    """The popup to show now, or null when it is off, blank or outside its time range."""
    row = await db.get(PopupAnnouncement, POPUP_ID)
    if row is None or not row.is_live(datetime.now(timezone.utc)):
        return None
    return row


@router.get("/admin/announcements/popup", response_model=PopupRead)
async def get_popup_settings(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PopupAnnouncement, POPUP_ID)
    return row or PopupRead(message="", is_active=False)


@router.put("/admin/announcements/popup", response_model=PopupRead)
async def update_popup(
    payload: PopupUpdate,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PopupAnnouncement, POPUP_ID)
    if row is None:
        row = PopupAnnouncement(id=POPUP_ID)
        db.add(row)
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    row.updated_by = admin.id
    await db.commit()
    await db.refresh(row)
    return row


# ── Banner presets ("Hazır metinler") ────────────────────────────────────────


@router.get("/admin/banner-presets", response_model=list[BannerPresetRead])
async def list_banner_presets(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(select(BannerPreset).order_by(BannerPreset.sort_order, BannerPreset.id))
    return rows.scalars().all()


@router.post("/admin/banner-presets", response_model=BannerPresetRead, status_code=status.HTTP_201_CREATED)
async def create_banner_preset(
    payload: BannerPresetWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    preset = BannerPreset(**payload.model_dump())
    db.add(preset)
    await db.commit()
    await db.refresh(preset)
    return preset


@router.put("/admin/banner-presets/{preset_id}", response_model=BannerPresetRead)
async def update_banner_preset(
    preset_id: int,
    payload: BannerPresetWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    preset = await db.get(BannerPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Hazır metin bulunamadı")
    for field, value in payload.model_dump().items():
        setattr(preset, field, value)
    await db.commit()
    await db.refresh(preset)
    return preset


@router.delete("/admin/banner-presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_banner_preset(
    preset_id: int,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    preset = await db.get(BannerPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Hazır metin bulunamadı")
    await db.delete(preset)
    await db.commit()
