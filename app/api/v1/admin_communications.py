""""İletişim & Bildirimler": bulk e-mail / SMS campaigns and the message log."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_admin_user, get_session_factory
from app.core.config import settings
from app.core.database import get_db
from app.models.notification import NotificationCampaign, NotificationLog
from app.models.user import User
from app.schemas.communication import (
    AudienceCount,
    CampaignCreate,
    CampaignRead,
    Channel,
    DriverStatus,
    NotificationLogRead,
    SmsSettingsRead,
    SmsSettingsWrite,
    SmsTemplateIO,
    SmsTestRequest,
    SmsTestResult,
)
from app.services import campaigns, sms_settings
from app.services.notify import dispatch
from app.services.notify.drivers import email_driver, env_netgsm
from app.services.notify.phone import normalize_tr_mobile

router = APIRouter(prefix="/admin/communications", tags=["admin-communications"])


@router.get("/status", response_model=DriverStatus)
async def driver_status(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """Which drivers are live: "mock" means nothing actually leaves."""
    sms = await dispatch.sms_driver_for(db)
    return DriverStatus(email=email_driver().name, sms=sms.name, sms_opt_out_text=settings.SMS_OPT_OUT_TEXT)


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


# ── SMS Ayarları ─────────────────────────────────────────────────────────────


async def _sms_settings_read(db: AsyncSession) -> SmsSettingsRead:
    stored = await sms_settings.load(db)
    env = env_netgsm()
    templates = {event: SmsTemplateIO(enabled=t.enabled, text=t.text) for event, t in stored.templates.items()}
    return SmsSettingsRead(
        usercode=stored.usercode,
        header=stored.header,
        password_set=bool(stored.password),
        env_usercode=env.usercode,
        env_header=env.header,
        env_password_set=bool(env.password),
        provider=(await dispatch.sms_driver_for(db)).name,
        order_placed=templates[sms_settings.ORDER_PLACED],
        order_shipped=templates[sms_settings.ORDER_SHIPPED],
        placeholders={event: list(names) for event, names in sms_settings.PLACEHOLDERS.items()},
    )


@router.get("/sms-settings", response_model=SmsSettingsRead)
async def get_sms_settings(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """The Netgsm account and the order messages. The password never leaves."""
    return await _sms_settings_read(db)


@router.put("/sms-settings", response_model=SmsSettingsRead)
async def put_sms_settings(
    payload: SmsSettingsWrite,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stored = await sms_settings.load(db)
    stored.usercode = payload.usercode
    stored.header = payload.header
    if payload.clear_password:
        stored.password = ""
    elif payload.password:
        stored.password = payload.password
    stored.templates = {
        sms_settings.ORDER_PLACED: sms_settings.SmsTemplate(payload.order_placed.enabled, payload.order_placed.text),
        sms_settings.ORDER_SHIPPED: sms_settings.SmsTemplate(payload.order_shipped.enabled, payload.order_shipped.text),
    }
    await sms_settings.save(db, stored, admin.id)
    await db.commit()
    return await _sms_settings_read(db)


@router.post("/sms-settings/test", response_model=SmsTestResult)
async def send_test_sms(
    payload: SmsTestRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """One SMS through the saved account, to check it before customers rely on it."""
    number = normalize_tr_mobile(payload.phone)
    if number is None:
        raise HTTPException(status_code=422, detail="Geçerli bir cep telefonu numarası girin (05XX XXX XX XX).")
    [log] = await dispatch.send_sms(
        db,
        recipients=[(number, admin.id)],
        text="Nargile Malzemesi: SMS ayarlarınız çalışıyor. Bu bir test mesajıdır.",
        purpose="sms_test",
    )
    await db.commit()
    return SmsTestResult(ok=log.status == "sent", provider=log.provider, error=log.error)
