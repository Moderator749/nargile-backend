"""Bulk e-mail / SMS: who a campaign reaches, and sending it.

Only customers who agreed to campaign messages on that channel
(`marketing_*_consent_at`) and can be reached on it are in an audience —
6563 sayılı Kanun asks for consent before commercial e-mail and SMS. Typed-in
recipients go only when the admin confirms they consented, and never to an
address that unsubscribed.

Every campaign e-mail carries its recipient's own "Abonelikten Çık" link and
the one-click List-Unsubscribe headers (services/unsubscribe). With a Resend
key (services/notify/drivers.resend_api_key) they go out through Resend's
batch endpoint, a hundred a call; without one, one by one through the
configured driver. Either way a campaign never mails an address twice: it
skips whoever it already reached, so one cut off by a restart is picked up
again (resume_stale, every few minutes) and finishes the rest.
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.notification import EMAIL, FAILED, SENT, EmailOptOut, NotificationCampaign, NotificationLog
from app.models.user import CustomerSegment, User, UserRole
from app.services import unsubscribe
from app.services.notify import dispatch
from app.services.notify.drivers import RESEND_BATCH_MAX, resend_api_key, resend_batch, resend_message
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
#: Stands in the e-mail for its recipient's own "Abonelikten Çık" address.
UNSUBSCRIBE_MARK = "%%UNSUBSCRIBE_URL%%"
#: Seconds between batch calls: Resend takes about two requests a second.
BATCH_PAUSE = 0.6
#: One-by-one sending saves (and says it is alive) after this many.
SAVE_EVERY = 25
#: A "sending" campaign whose heartbeat is this old was cut off.
STALE_AFTER = timedelta(minutes=3)
#: Older than this, an interrupted campaign is left alone: a day later the
#: news is stale, and Resend no longer remembers a batch it already took.
RESUME_WITHIN = timedelta(hours=20)


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
        if channel == EMAIL and found:
            # Customers' own consent decides for them; a typed-in address has
            # only this to say it left.
            gone = await db.scalars(select(EmailOptOut.email).where(EmailOptOut.email.in_(list(found))))
            for address in gone:
                found.pop(address, None)
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
    """(text, html), each closing with why it came and the way out
    (UNSUBSCRIBE_MARK, for `personal` to fill in)."""
    why = f"Bu e-postayı {settings.SHOP_NAME} kampanya iletilerine izin verdiğiniz için aldınız."
    if body_format == "html":
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"[ \t]+", " ", text).strip()
        html = body
    else:
        text, html = body, dispatch.text_to_html(body)
    html += (
        '<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">'
        f'<p style="font-size:12px;color:#6b7280">{why} '
        f'Bu iletileri artık almak istemiyorsanız: <a href="{UNSUBSCRIBE_MARK}" '
        'style="color:#6b7280;text-decoration:underline">Abonelikten Çık</a></p>'
    )
    return f"{text}\n\n—\n{why}\nAbonelikten çıkmak için: {UNSUBSCRIBE_MARK}", html


def personal(text: str, html: str, address: str) -> tuple[str, str]:
    """The e-mail as it goes to one address: its own unsubscribe link in."""
    link = unsubscribe.page_url(address)
    return text.replace(UNSUBSCRIBE_MARK, link), html.replace(UNSUBSCRIBE_MARK, link)


def segments_for(channel: str, body: str) -> int | None:
    return sms_parts(sms_text(body))[2] if channel != EMAIL else None


async def run_campaign(
    factory: async_sessionmaker, campaign_id: int, targets: list[tuple[str, object]] | None = None
) -> None:
    """Send a campaign in its own session (after the request returns, or on
    resuming one): to `targets`, or — resuming — its audience worked out again."""
    async with factory() as db:
        campaign = await db.get(NotificationCampaign, campaign_id)
        if campaign is None:
            return
        if targets is None:
            targets = await recipients(db, campaign.channel, campaign.audience, campaign.manual_recipients)
        campaign.status = "sending"
        campaign.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()
        try:
            if campaign.channel == EMAIL:
                await _send_emails(db, campaign, targets)
                await _count_emails(db, campaign)
            else:
                logs = await dispatch.send_sms(
                    db, recipients=targets, text=sms_text(campaign.body), purpose=PURPOSE, campaign_id=campaign.id
                )
                campaign.sent = sum(log.status == "sent" for log in logs)
                campaign.failed = sum(log.status == "failed" for log in logs)
            campaign.status = "done" if campaign.sent else "failed"
        except Exception:  # noqa: BLE001 — a crash mid-way still closes the campaign
            logger.exception("Campaign %s stopped", campaign_id)
            await db.rollback()
            campaign = await db.get(NotificationCampaign, campaign_id)
            if campaign.channel == EMAIL:
                await _count_emails(db, campaign)
            campaign.status = "failed"
        campaign.finished_at = datetime.now(timezone.utc)
        await db.commit()


async def _send_emails(db: AsyncSession, campaign: NotificationCampaign, targets: list[tuple[str, object]]) -> None:
    reached = set(
        await db.scalars(
            select(NotificationLog.recipient).where(NotificationLog.campaign_id == campaign.id, NotificationLog.status == SENT)
        )
    )
    todo = [(address, user_id) for address, user_id in targets if address not in reached]
    subject = campaign.subject or ""
    text, html = email_parts(campaign.body, campaign.body_format)

    key = resend_api_key()
    if key is None:
        for n, (address, user_id) in enumerate(todo, 1):
            own_text, own_html = personal(text, html, address)
            await dispatch.send_email(
                db, to=address, subject=subject, text=own_text, html=own_html, purpose=PURPOSE,
                user_id=user_id, campaign_id=campaign.id, headers=unsubscribe.headers(address),
            )
            if n % SAVE_EVERY == 0:
                campaign.heartbeat_at = datetime.now(timezone.utc)
                await db.commit()
        return

    for start in range(0, len(todo), RESEND_BATCH_MAX):
        if start:
            await asyncio.sleep(BATCH_PAUSE)
        batch = todo[start : start + RESEND_BATCH_MAX]
        rows = [
            NotificationLog(
                channel=EMAIL, recipient=address, subject=subject[:200], purpose=PURPOSE, user_id=user_id,
                campaign_id=campaign.id, provider="resend",
            )
            for address, user_id in batch
        ]
        db.add_all(rows)
        await db.flush()
        messages = [
            resend_message(address, subject, *personal(text, html, address), unsubscribe.headers(address))
            for address, _ in batch
        ]
        # The same recipients make the same key: a batch Resend took just
        # before a restart is not delivered again when the campaign resumes.
        fingerprint = hashlib.sha256("\n".join(address for address, _ in batch).encode()).hexdigest()[:40]
        deliveries = await resend_batch(key, messages, f"campaign-{campaign.id}-{fingerprint}")
        for row, delivery in zip(rows, deliveries):
            dispatch.settle(row, delivery)
        campaign.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()


async def _count_emails(db: AsyncSession, campaign: NotificationCampaign) -> None:
    """Sent and failed by address, from the log: an address that failed and
    then went through on a resume counts once, as sent."""
    rows = (
        await db.execute(
            select(NotificationLog.recipient, NotificationLog.status).where(NotificationLog.campaign_id == campaign.id)
        )
    ).all()
    sent = {recipient for recipient, status in rows if status == SENT}
    failed = {recipient for recipient, status in rows if status == FAILED} - sent
    campaign.sent, campaign.failed = len(sent), len(failed)


async def claim(db: AsyncSession, campaign_id: int) -> bool:
    """Take a campaign to send again, unless it is sending right now (its
    heartbeat fresh). Committed."""
    now = datetime.now(timezone.utc)
    claimed = await db.scalar(
        update(NotificationCampaign)
        .where(
            NotificationCampaign.id == campaign_id,
            or_(
                NotificationCampaign.status != "sending",
                NotificationCampaign.heartbeat_at.is_(None),
                NotificationCampaign.heartbeat_at < now - STALE_AFTER,
            ),
        )
        .values(status="sending", heartbeat_at=now, finished_at=None)
        .returning(NotificationCampaign.id)
    )
    await db.commit()
    return claimed is not None


async def resume_stale(factory: async_sessionmaker) -> int:
    """Pick up the e-mail campaigns a restart cut off: "sending" with a quiet
    heartbeat, or queued and never started. Each is claimed by moving its
    heartbeat in the same statement, so of two workers only one takes it."""
    now = datetime.now(timezone.utc)
    async with factory() as db:
        claimed = (
            await db.scalars(
                update(NotificationCampaign)
                .where(
                    NotificationCampaign.channel == EMAIL,
                    NotificationCampaign.created_at > now - RESUME_WITHIN,
                    or_(
                        and_(NotificationCampaign.status == "sending", NotificationCampaign.heartbeat_at < now - STALE_AFTER),
                        and_(NotificationCampaign.status == "queued", NotificationCampaign.created_at < now - STALE_AFTER),
                    ),
                )
                .values(heartbeat_at=now)
                .returning(NotificationCampaign.id)
            )
        ).all()
        await db.commit()
    for campaign_id in claimed:
        logger.warning("Campaign %s was cut off; resuming it", campaign_id)
        await run_campaign(factory, campaign_id)
    return len(claimed)
