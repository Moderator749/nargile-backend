"""Ticari elektronik ileti onayı given outside the preferences switches.

Checkout's box and the one-time card only ever give consent: an unticked box
is not a withdrawal, which is the switches' job in "Hesabım". A consent
already on file keeps the moment it was first given.
"""

from datetime import datetime, timezone

from app.models.user import User


def grant(user: User, *, email: bool, sms: bool, now: datetime | None = None) -> None:
    """Record the channels the customer agreed to, and that they were asked. Not committed."""
    now = now or datetime.now(timezone.utc)
    if email and user.marketing_email_consent_at is None:
        user.marketing_email_consent_at = now
    if sms and user.marketing_sms_consent_at is None:
        user.marketing_sms_consent_at = now
    user.consent_prompted_at = now


def prompt_due(user: User) -> bool:
    """Whether the one-time card should ask: never asked, and not both given."""
    return user.consent_prompted_at is None and not (
        user.marketing_email_consent_at and user.marketing_sms_consent_at
    )
