""""İletişim & Bildirimler": bulk e-mail / SMS campaigns and the message log."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_admin_user
from app.core.config import settings
from app.core.database import async_session, get_db
from app.models.notification import NotificationCampaign, NotificationLog
from app.models.user import User
from app.schemas.communication import (
    AudienceCount,
    CampaignCreate,
    CampaignRead,
    Channel,
    DriverStatus,
    NotificationLogRead,
)
from app.services import campaigns
from app.services.notify.drivers import email_driver, sms_driver

router = APIRouter(prefix="/admin/communications", tags=["admin-communications"])


def get_session_factory() -> async_sessionmaker:
    """Where a campaign sends from once the request is over (the tests swap it)."""
    return async_session


@router.get("/status", response_model=DriverStatus)
async def driver_status(_admin: User = Depends(get_current_admin_user)):
    """Which drivers are live: "mock" means nothing actually leaves."""
    return DriverStatus(email=email_driver().name, sms=sms_driver().name, sms_opt_out_text=settings.SMS_OPT_OUT_TEXT)


@router.get("/audiences", response_model=list[AudienceCount])
async def audiences(
    channel: Channel, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    return await campaigns.audience_counts(db, channel)


@router.post("/campaigns", response_model=CampaignRead, status_code=status.HTTP_202_ACCEPTED)
async def create_campaign(
    payload: CampaignCreate,
    background: BackgroundTasks,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    factory: async_sessionmaker = Depends(get_session_factory),
):
    targets = await campaigns.recipients(db, payload.channel, payload.audience, payload.manual_recipients)
    if not targets:
        raise HTTPException(
            status_code=422,
            detail="Bu kitlede ileti gönderilebilecek alıcı yok (izin vermiş ve iletişim bilgisi geçerli müşteri bulunamadı).",
        )
    campaign = NotificationCampaign(
        channel=payload.channel,
        audience=payload.audience,
        subject=payload.subject,
        body=payload.body,
        body_format=payload.body_format,
        sms_segments=campaigns.segments_for(payload.channel, payload.body),
        total=len(targets),
        created_by=admin.id,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    background.add_task(campaigns.run_campaign, factory, campaign.id, targets)
    return campaign


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(NotificationCampaign).order_by(NotificationCampaign.created_at.desc(), NotificationCampaign.id.desc()).limit(limit)
    )
    return rows.scalars().all()


@router.get("/logs", response_model=list[NotificationLogRead])
async def list_logs(
    channel: Channel | None = None,
    limit: int = Query(50, ge=1, le=200),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(NotificationLog).order_by(NotificationLog.created_at.desc(), NotificationLog.id.desc()).limit(limit)
    if channel:
        stmt = stmt.where(NotificationLog.channel == channel)
    return (await db.execute(stmt)).scalars().all()
