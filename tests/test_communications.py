"""Communication: password reset by e-mail / SMS, mock delivery and its log,
bulk campaigns that reach only consenting customers, consent preferences."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import password_reset as reset_api
from app.api.v1.admin_communications import get_session_factory
from app.core.security import hash_password, verify_password
from app.main import app
from app.models.notification import NotificationCampaign, NotificationLog, PasswordResetToken
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services import password_reset
from app.services.notify.phone import normalize_tr_mobile
from app.services.notify.sms_segments import sms_parts
from tests.conftest import auth_header

REQUEST = "/api/v1/auth/password-reset/request"
CONFIRM = "/api/v1/auth/password-reset/confirm"
CHECK = "/api/v1/auth/password-reset/check"
CAMPAIGNS = "/api/v1/admin/communications/campaigns"


@pytest.fixture(autouse=True)
def fresh_ip_limit():
    reset_api._hits.clear()
    yield
    reset_api._hits.clear()


@pytest.fixture
def session_factory(db_session: AsyncSession):
    """Campaigns send in the test database, from the test's own engine."""
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    app.dependency_overrides[get_session_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_session_factory, None)


async def _customer(db: AsyncSession, *, phone: str | None = None, email_ok=False, sms_ok=False, segment=SegmentType.RETAIL) -> User:
    seg_id = await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == segment))
    now = datetime.now(timezone.utc)
    user = User(
        email=f"cust-{uuid.uuid4().hex[:8]}@test.com", hashed_password=hash_password("eski-sifre-1"),
        role=UserRole.CUSTOMER, segment_id=seg_id, is_active=True, phone=phone,
        marketing_email_consent_at=now if email_ok else None, marketing_sms_consent_at=now if sms_ok else None,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _mobile() -> str:
    return f"0532 {uuid.uuid4().int % 10**7:07d}"


# ── Units ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [("0532 123 45 67", "905321234567"), ("+90 (532) 123-45-67", "905321234567"), ("5321234567", "905321234567"),
     ("0212 123 45 67", None), ("+49 151 1234567", None), ("", None)],
)
def test_turkish_mobiles_are_normalised(raw: str, want: str | None):
    assert normalize_tr_mobile(raw) == want


def test_sms_parts_follow_gsm_and_ucs2():
    assert sms_parts("a" * 160) == ("GSM-7", 160, 1)
    assert sms_parts("a" * 161) == ("GSM-7", 161, 2)
    assert sms_parts("€" * 80) == ("GSM-7", 160, 1)  # extension characters count twice
    assert sms_parts("ş" * 70) == ("UCS-2", 70, 1)  # Turkish letters leave GSM-7
    assert sms_parts("ş" * 71) == ("UCS-2", 71, 2)
    assert sms_parts("") == ("GSM-7", 0, 0)


# ── Password reset ───────────────────────────────────────────────────────────


async def test_reset_by_email_link_is_single_use(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    user = await _customer(db_session)
    monkeypatch.setattr(password_reset.secrets, "token_urlsafe", lambda n: "link-token-" + user.email)
    resp = await client.post(REQUEST, json={"channel": "email", "identifier": user.email.upper()})
    assert resp.status_code == 202

    row = await db_session.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    assert row.token_hash == password_reset.hash_link_token("link-token-" + user.email)  # only the hash is kept
    assert abs(row.expires_at - row.created_at - timedelta(minutes=30)) < timedelta(seconds=5)
    log = await db_session.scalar(select(NotificationLog).where(NotificationLog.user_id == user.id))
    assert (log.channel, log.purpose, log.provider, log.status) == ("email", "password_reset", "mock", "sent")

    token = "link-token-" + user.email
    assert (await client.post(CHECK, json={"token": token})).json() == {"valid": True}
    ok = await client.post(CONFIRM, json={"token": token, "new_password": "yeni-sifre-123"})
    assert ok.status_code == 200, ok.text
    await db_session.refresh(user)
    assert verify_password("yeni-sifre-123", user.hashed_password)
    # Spent: neither the link nor its check works again.
    assert (await client.post(CONFIRM, json={"token": token, "new_password": "baska-sifre-1"})).status_code == 400
    assert (await client.post(CHECK, json={"token": token})).json() == {"valid": False}


async def test_an_expired_or_superseded_link_is_refused(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    user = await _customer(db_session)
    tokens = iter(["ilk-" + user.email, "ikinci-" + user.email])
    monkeypatch.setattr(password_reset.secrets, "token_urlsafe", lambda n: next(tokens))
    await client.post(REQUEST, json={"channel": "email", "identifier": user.email})
    await client.post(REQUEST, json={"channel": "email", "identifier": user.email})
    # The newer request voided the first link.
    assert (await client.post(CONFIRM, json={"token": "ilk-" + user.email, "new_password": "yeni-sifre-123"})).status_code == 400

    await db_session.execute(
        update(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()
    assert (await client.post(CONFIRM, json={"token": "ikinci-" + user.email, "new_password": "yeni-sifre-123"})).status_code == 400


async def test_reset_by_sms_code_with_an_attempt_limit(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    phone = _mobile()
    user = await _customer(db_session, phone=phone)
    monkeypatch.setattr(password_reset.secrets, "randbelow", lambda n: 123456)
    assert (await client.post(REQUEST, json={"channel": "sms", "identifier": "+90 " + phone[1:]})).status_code == 202
    log = await db_session.scalar(select(NotificationLog).where(NotificationLog.user_id == user.id))
    assert (log.channel, log.recipient, log.status) == ("sms", normalize_tr_mobile(phone), "sent")

    wrong = {"phone": phone, "code": "000000", "new_password": "yeni-sifre-123"}
    for _ in range(4):
        assert (await client.post(CONFIRM, json=wrong)).status_code == 400
    ok = await client.post(CONFIRM, json={**wrong, "code": "123456"})
    assert ok.status_code == 200, ok.text

    # A new code, guessed wrong five times, is dead even for the right digits.
    reset_api._hits.clear()  # the per-address limit is tested on its own
    await client.post(REQUEST, json={"channel": "sms", "identifier": phone})
    for _ in range(5):
        await client.post(CONFIRM, json=wrong)
    assert (await client.post(CONFIRM, json={**wrong, "code": "123456"})).status_code == 400


async def test_the_request_does_not_tell_who_has_an_account(client: AsyncClient, db_session: AsyncSession):
    known = await _customer(db_session)
    a = await client.post(REQUEST, json={"channel": "email", "identifier": known.email})
    b = await client.post(REQUEST, json={"channel": "email", "identifier": "kimse-yok@test.com"})
    c = await client.post(REQUEST, json={"channel": "sms", "identifier": "0532 000 00 00"})
    assert a.status_code == b.status_code == c.status_code == 202
    assert a.json() == b.json() == c.json()
    assert await db_session.scalar(select(NotificationLog.id).where(NotificationLog.recipient == "kimse-yok@test.com")) is None


async def test_requests_are_rate_limited(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    for _ in range(5):
        await client.post(REQUEST, json={"channel": "email", "identifier": user.email})
    tokens = (await db_session.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))).scalars().all()
    assert len(tokens) == 3  # PASSWORD_RESET_MAX_REQUESTS per window
    reset_api._hits.clear()
    for _ in range(reset_api.IP_LIMIT):
        await client.post(REQUEST, json={"channel": "email", "identifier": "x@test.com"})
    assert (await client.post(REQUEST, json={"channel": "email", "identifier": "x@test.com"})).status_code == 429


async def test_a_weak_or_incomplete_confirmation_is_refused(client: AsyncClient):
    assert (await client.post(CONFIRM, json={"token": "x", "new_password": "kisa"})).status_code == 422
    assert (await client.post(CONFIRM, json={"new_password": "yeni-sifre-123"})).status_code == 422
    assert (await client.post(CONFIRM, json={"phone": "05321234567", "code": "12", "new_password": "yeni-sifre-123"})).status_code == 422


# ── Campaigns ────────────────────────────────────────────────────────────────


async def test_an_sms_campaign_reaches_only_consenting_customers(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, session_factory
):
    yes = await _customer(db_session, phone=_mobile(), sms_ok=True, segment=SegmentType.CAFE)
    no = await _customer(db_session, phone=_mobile(), sms_ok=False, segment=SegmentType.CAFE)
    no_phone = await _customer(db_session, sms_ok=True, segment=SegmentType.CAFE)
    h = auth_header(admin_user)

    counts = {a["key"]: a for a in (await client.get("/api/v1/admin/communications/audiences", headers=h, params={"channel": "sms"})).json()}
    assert counts["CAFE"]["total"] >= 3 and counts["CAFE"]["eligible"] >= 1

    resp = await client.post(CAMPAIGNS, headers=h, json={"channel": "sms", "audience": "CAFE", "body": "Kafe kampanyası başladı!"})
    assert resp.status_code == 202, resp.text
    campaign = resp.json()
    assert campaign["sms_segments"] == 1

    yes_id, no_id, no_phone_id = yes.id, no.id, no_phone.id
    db_session.expire_all()
    done = await db_session.get(NotificationCampaign, campaign["id"])
    logs = (await db_session.execute(select(NotificationLog).where(NotificationLog.campaign_id == done.id))).scalars().all()
    reached = {log.user_id for log in logs}
    assert yes_id in reached and no_id not in reached and no_phone_id not in reached
    assert (done.status, done.sent, done.failed, done.total) == ("done", len(logs), 0, len(logs))
    assert all((log.provider, log.status, log.purpose) == ("mock", "sent", "campaign") for log in logs)

    history = (await client.get(CAMPAIGNS, headers=h)).json()
    assert history[0]["id"] == done.id and history[0]["status"] == "done"
    recent = (await client.get("/api/v1/admin/communications/logs", headers=h, params={"channel": "sms"})).json()
    assert any(r["campaign_id"] == done.id for r in recent)


async def test_an_email_campaign_to_typed_in_addresses(client: AsyncClient, db_session: AsyncSession, admin_user: User, session_factory):
    h = auth_header(admin_user)
    body = {"channel": "email", "audience": "manual", "manual_recipients": ["A@test.com", "a@test.com", "gecersiz"],
            "subject": "Yeni ürünler", "body": "<h1>Merhaba</h1><p>Yeni ürünler geldi.</p>", "body_format": "html"}
    # The admin has to vouch for typed-in recipients' consent.
    assert (await client.post(CAMPAIGNS, headers=h, json=body)).status_code == 422
    resp = await client.post(CAMPAIGNS, headers=h, json={**body, "consent_confirmed": True})
    assert resp.status_code == 202, resp.text
    assert resp.json()["total"] == 1  # deduplicated, the invalid one dropped
    db_session.expire_all()
    log = await db_session.scalar(select(NotificationLog).where(NotificationLog.campaign_id == resp.json()["id"]))
    assert (log.recipient, log.subject, log.status) == ("a@test.com", "Yeni ürünler", "sent")


async def test_campaign_rules(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, session_factory):
    h = auth_header(admin_user)
    assert (await client.post(CAMPAIGNS, headers=h, json={"channel": "email", "audience": "all", "body": "x"})).status_code == 422  # no subject
    assert (await client.post(CAMPAIGNS, headers=h, json={"channel": "sms", "audience": "all", "body": "x" * 919})).status_code == 422
    # An audience nobody in it agreed to: nothing to send.
    await db_session.execute(update(User).where(User.role == UserRole.CUSTOMER).values(marketing_sms_consent_at=None))
    await db_session.commit()
    assert (await client.post(CAMPAIGNS, headers=h, json={"channel": "sms", "audience": "DEALER", "body": "x"})).status_code == 422
    customer = auth_header(customer_user)
    assert (await client.post(CAMPAIGNS, headers=customer, json={"channel": "sms", "audience": "all", "body": "x"})).status_code == 403
    assert (await client.get("/api/v1/admin/communications/status", headers=customer)).status_code == 403
    assert (await client.get("/api/v1/admin/communications/status", headers=h)).json()["sms"] == "mock"


async def test_customers_give_and_withdraw_consent(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    h = auth_header(user)
    url = "/api/v1/me/communication-preferences"
    assert (await client.get(url, headers=h)).json() == {"email": False, "sms": False}
    assert (await client.put(url, headers=h, json={"email": True, "sms": True})).status_code == 200
    await db_session.refresh(user)
    given = user.marketing_email_consent_at
    assert given is not None and user.marketing_sms_consent_at is not None
    # Saving again keeps the date consent was given; withdrawing clears it.
    await client.put(url, headers=h, json={"email": True, "sms": False})
    await db_session.refresh(user)
    assert (user.marketing_email_consent_at, user.marketing_sms_consent_at) == (given, None)
