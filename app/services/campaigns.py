"""Bulk e-mail / SMS: who a campaign reaches, and sending it.

Only customers who agreed to campaign messages on that channel
(`marketing_*_consent_at`) and can be reached on it are in an audience —
6563 sayılı Kanun asks for consent before commercial e-mail and SMS. Typed-in
recipients go only when the admin confirms they consented.
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.notification import EMAIL, NotificationCampaign
from app.models.user import CustomerSegment, User, UserRole
from app.services.notify import dispatch
from app.services.notify.phone import normalize_tr_mobile
from app.services.notify.sms_segments import sms_parts

logger = logging.getLogger("notify")

AUDIENCES = {
    "all": "Tüm müşteriler",
    "RETAIL": "Bireysel (perakende)",
    "CAFE": "Kafe",
    "WHOLESALE": "Toptan",
    "DEALER": "Bayi",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PURPOSE = "campaign"


def _customers(audience: str):
    stmt = select(User).where(User.role == UserRole.CUSTOMER, User.is_active.is_(True))
    if audience != "all":
        stmt = stmt.join(CustomerSegment, CustomerSegment.id == User.segment_id).where(CustomerSegment.code == audience)
    return stmt


def _consented(stmt, channel: str):
    column = User.marketing_email_consent_at if channel == EMAIL else User.marketing_sms_consent_at
    return stmt.where(column.is_not(None))


async def audience_counts(db: AsyncSession, channel: str) -> list[dict]:
    out = []
    for key, label in AUDIENCES.items():
        total = await db.scalar(select(func.count()).select_from(_customers(key).subquery())) or 0
        out.append({"key": key, "label": label, "total": total, "eligible": len(await recipients(db, channel, key))})
    return out


async def recipients(db: AsyncSession, channel: str, audience: str, manual: list[str] | None = None) -> list[tuple[str, object]]:
    """(address or number, user id) pairs, deduplicated; typed-in ones have no user."""
    found: dict[str, object] = {}
    if audience == "manual":
        for raw in manual or []:
            address = raw.strip().lower() if channel == EMAIL else normalize_tr_mobile(raw)
            if address and (channel != EMAIL or _EMAIL.match(address)):
                found.setdefault(address, None)
    else:
        for user in (await db.execute(_consented(_customers(audience), channel))).scalars():
            address = user.email.strip().lower() if channel == EMAIL else normalize_tr_mobile(user.phone)
            if address and (channel != EMAIL or _EMAIL.match(address)):
                found.setdefault(address, user.id)
    return list(found.items())


def sms_text(body: str) -> str:
    """The campaign SMS with the opt-out line the rules ask for, when one is set."""
    opt_out = settings.SMS_OPT_OUT_TEXT.strip()
    return f"{body}\n{opt_out}" if opt_out else body


def email_parts(body: str, body_format: str) -> tuple[str, str]:
    """(text, html), each closing with how to stop receiving these."""
    footer = (
        f"Bu e-postayı {settings.SHOP_NAME} kampanya iletilerine izin verdiğiniz için aldınız. "
        f"İzninizi hesabınızdan istediğiniz zaman kaldırabilirsiniz: {settings.SHOP_URL}/account"
    )
    if body_format == "html":
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"[ \t]+", " ", text).strip()
        html = body
    else:
        text, html = body, dispatch.text_to_html(body)
    html += f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0"><p style="font-size:12px;color:#6b7280">{footer}</p>'
    return f"{text}\n\n—\n{footer}", html


def segments_for(channel: str, body: str) -> int | None:
    return sms_parts(sms_text(body))[2] if channel != EMAIL else None


async def run_campaign(factory: async_sessionmaker, campaign_id: int, targets: list[tuple[str, object]]) -> None:
    """Send a queued campaign in its own session (after the request returns)."""
    async with factory() as db:
        campaign = await db.get(NotificationCampaign, campaign_id)
        if campaign is None:
            return
        campaign.status = "sending"
        await db.commit()
        try:
            if campaign.channel == EMAIL:
                text, html = email_parts(campaign.body, campaign.body_format)
                for n, (address, user_id) in enumerate(targets, 1):
                    log = await dispatch.send_email(
                        db, to=address, subject=campaign.subject or "", text=text, html=html,
                        purpose=PURPOSE, user_id=user_id, campaign_id=campaign.id,
                    )
                    campaign.sent += log.status == "sent"
                    campaign.failed += log.status == "failed"
                    if n % 25 == 0:
                        await db.commit()
            else:
                logs = await dispatch.send_sms(
                    db, recipients=targets, text=sms_text(campaign.body), purpose=PURPOSE, campaign_id=campaign.id
                )
                campaign.sent = sum(log.status == "sent" for log in logs)
                campaign.failed = sum(log.status == "failed" for log in logs)
            campaign.status = "done" if campaign.sent else "failed"
        except Exception:  # noqa: BLE001 — a crash mid-way still closes the campaign
            logger.exception("Campaign %s stopped", campaign_id)
            campaign.status = "failed"
        campaign.finished_at = datetime.now(timezone.utc)
        await db.commit()
