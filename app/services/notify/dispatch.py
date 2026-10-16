"""Send and log: every message gets a `notification_logs` row, pending first,
then sent or failed with the driver's reason."""

import html as html_lib
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import EMAIL, FAILED, SENT, SMS, NotificationLog
from app.services.notify.drivers import Delivery, email_driver, sms_driver

# Recipients per SMS gateway call in a campaign.
SMS_BATCH = 100


def text_to_html(text: str) -> str:
    """Plain text as a minimal HTML body: escaped, paragraphs and line breaks kept."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{html_lib.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def _settle(log: NotificationLog, delivery: Delivery) -> None:
    log.provider = delivery.provider
    log.status = SENT if delivery.ok else FAILED
    log.error = delivery.error
    log.provider_ref = delivery.ref
    if delivery.ok:
        log.sent_at = datetime.now(timezone.utc)


async def send_email(
    db: AsyncSession,
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    purpose: str,
    user_id: uuid.UUID | None = None,
    campaign_id: int | None = None,
) -> NotificationLog:
    """One e-mail, logged. The caller commits."""
    driver = email_driver()
    log = NotificationLog(
        channel=EMAIL, recipient=to, subject=subject[:200], purpose=purpose, user_id=user_id,
        campaign_id=campaign_id, provider=driver.name,
    )
    db.add(log)
    await db.flush()
    _settle(log, await driver.send(to, subject, text, html or text_to_html(text)))
    return log


async def send_sms(
    db: AsyncSession,
    *,
    recipients: list[tuple[str, uuid.UUID | None]],
    text: str,
    purpose: str,
    campaign_id: int | None = None,
) -> list[NotificationLog]:
    """The same SMS to (number, user id) pairs — numbers normalised — in
    batches, one log row each. The caller commits."""
    driver = sms_driver()
    logs: list[NotificationLog] = []
    for start in range(0, len(recipients), SMS_BATCH):
        batch = recipients[start : start + SMS_BATCH]
        rows = [
            NotificationLog(
                channel=SMS, recipient=number, user_id=user_id, purpose=purpose, campaign_id=campaign_id,
                provider=driver.name,
            )
            for number, user_id in batch
        ]
        db.add_all(rows)
        await db.flush()
        delivery = await driver.send([n for n, _ in batch], text)
        for row in rows:
            _settle(row, delivery)
        logs += rows
    return logs
