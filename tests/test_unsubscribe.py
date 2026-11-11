"""One-click unsubscribe from campaign e-mail, and the Resend batch sender
that resumes a campaign without mailing anyone twice."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.security import hash_password
from app.models.notification import EmailOptOut, NotificationCampaign, NotificationLog
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services import campaigns, unsubscribe
from app.services.notify import drivers
from tests.conftest import auth_header

UNSUBSCRIBE = "/api/v1/newsletter/unsubscribe"
CAMPAIGNS = "/api/v1/admin/communications/campaigns"


@pytest.fixture
def factory(db_session: AsyncSession):
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


@pytest.fixture
def resend(monkeypatch):
    """Resend's batch endpoint, faked: every call recorded, answered from `replies` first."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.resend.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "re_test_key")
    monkeypatch.setattr(campaigns, "BATCH_PAUSE", 0)
    monkeypatch.setattr(drivers, "RESEND_BACKOFF", 0)
    state = {"calls": [], "replies": []}

    async def post(url, *, headers, json):
        state["calls"].append({"url": url, "headers": headers, "json": json})
        request = httpx.Request("POST", url)
        if state["replies"]:
            status, extra = state["replies"].pop(0)
            return httpx.Response(status, headers=extra, json={"message": "slow down"}, request=request)
        return httpx.Response(200, json={"data": [{"id": f"re-{uuid.uuid4().hex[:6]}"} for _ in json]}, request=request)

    monkeypatch.setattr(drivers, "_resend_post", post)
    return state


async def _customer(db: AsyncSession, email: str | None = None, *, consent=True) -> User:
    seg_id = await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))
    user = User(
        email=email or f"abone-{uuid.uuid4().hex[:8]}@test.com", hashed_password=hash_password("eski-sifre-1"),
        role=UserRole.CUSTOMER, segment_id=seg_id, is_active=True,
        marketing_email_consent_at=datetime.now(timezone.utc) if consent else None,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _campaign(db: AsyncSession, admin: User, addresses: list[str], **extra) -> NotificationCampaign:
    campaign = NotificationCampaign(
        channel="email", audience="manual", manual_recipients=addresses, subject="11.11 Fırsatları",
        body="<p>Kaçırmayın</p>", body_format="html", total=len(addresses), created_by=admin.id, **extra,
    )
    db.add(campaign)
    await db.commit()
    return campaign


def test_a_token_names_its_address_and_cannot_be_forged():
    token = unsubscribe.token_for(" Ayse@Example.com ")
    assert unsubscribe.email_from(token) == "ayse@example.com"
    payload, signature = token.split(".")
    other = unsubscribe.token_for("baska@example.com").split(".")[0]
    assert unsubscribe.email_from(f"{other}.{signature}") is None  # someone else's address, this signature
    assert unsubscribe.email_from(f"{payload}.{signature[:-2]}xx") is None
    assert unsubscribe.email_from("bozuk") is None and unsubscribe.email_from("!!.??") is None


def test_the_headers_are_one_click_on_the_canonical_host():
    headers = unsubscribe.headers("ayse@example.com")
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    https, mailto = headers["List-Unsubscribe"].split(", ")
    assert https == f"<https://www.nargilemalzemesi.com/api/backend/api/v1/newsletter/unsubscribe?token={unsubscribe.token_for('ayse@example.com')}>"
    assert mailto.startswith(f"<mailto:{settings.MAIL_FROM}?subject=")


async def test_one_click_unsubscribes_without_logging_in(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session, f"Tek-{uuid.uuid4().hex[:6]}@Test.com")
    token = unsubscribe.token_for(user.email)
    # The mail app's button: RFC 8058's form body, no session.
    resp = await client.post(UNSUBSCRIBE, params={"token": token}, content=b"List-Unsubscribe=One-Click",
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"email": user.email.lower()}
    await db_session.refresh(user)
    assert user.marketing_email_consent_at is None
    row = await db_session.get(EmailOptOut, user.email.lower())
    assert row.source == "one_click"
    # Again (the page, a second click): still fine, nothing new.
    assert (await client.post(UNSUBSCRIBE, params={"token": token})).status_code == 200
    assert (await db_session.get(EmailOptOut, user.email.lower())).source == "one_click"


async def test_a_bad_token_changes_nothing(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    forged = unsubscribe.token_for(user.email).split(".")[0] + ".AAAAAAAAAAAAAAAAAAAAAAAA"
    resp = await client.post(UNSUBSCRIBE, params={"token": forged})
    assert resp.status_code == 400
    await db_session.refresh(user)
    assert user.marketing_email_consent_at is not None


async def test_opening_the_api_link_only_leads_to_the_page(client: AsyncClient, db_session: AsyncSession):
    # A link scanner GETs every URL in a mail: that must not unsubscribe anyone.
    user = await _customer(db_session)
    token = unsubscribe.token_for(user.email)
    resp = await client.get(UNSUBSCRIBE, params={"token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"https://www.nargilemalzemesi.com/abonelikten-cik?token={token}"
    await db_session.refresh(user)
    assert user.marketing_email_consent_at is not None


async def test_an_unsubscribed_address_is_left_out_of_typed_in_lists(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    gone = f"gitti-{uuid.uuid4().hex[:6]}@test.com"
    await unsubscribe.opt_out(db_session, gone, "link")
    targets = await campaigns.recipients(db_session, "email", "manual", [gone.upper(), "kalan@test.com"])
    assert [address for address, _ in targets] == ["kalan@test.com"]


async def test_a_campaign_goes_through_resend_in_batches_with_its_own_links(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, resend
):
    addresses = [f"toplu-{n}-{uuid.uuid4().hex[:4]}@test.com" for n in range(230)]
    # The first call is refused for the rate, then goes through.
    resend["replies"] = [(429, {"retry-after": "0"})]
    resp = await client.post(CAMPAIGNS, headers=auth_header(admin_user), json={
        "channel": "email", "audience": "manual", "manual_recipients": addresses, "consent_confirmed": True,
        "subject": "11.11 Fırsatları", "body": "<p>Kaçırmayın</p>", "body_format": "html",
    })
    assert resp.status_code == 202, resp.text

    calls = resend["calls"]
    assert [len(c["json"]) for c in calls] == [100, 100, 100, 30]  # the first 100 twice: the 429, then its retry
    assert calls[0]["headers"]["Idempotency-Key"] == calls[1]["headers"]["Idempotency-Key"]
    assert calls[0]["headers"]["Authorization"] == "Bearer re_test_key"  # the SMTP password, Resend's own key
    message = calls[1]["json"][0]
    link = unsubscribe.page_url(message["to"][0])
    assert link in message["html"] and "Abonelikten Çık" in message["html"] and link in message["text"]
    assert "%%" not in message["html"]
    assert message["headers"] == unsubscribe.headers(message["to"][0])
    assert message["from"] == f"{settings.MAIL_FROM_NAME} <{settings.MAIL_FROM}>"

    db_session.expire_all()
    campaign = await db_session.get(NotificationCampaign, resp.json()["id"])
    assert (campaign.status, campaign.sent, campaign.failed, campaign.total) == ("done", 230, 0, 230)
    assert campaign.manual_recipients == addresses
    logs = (await db_session.execute(select(NotificationLog).where(NotificationLog.campaign_id == campaign.id))).scalars().all()
    assert len(logs) == 230 and all(log.provider == "resend" and log.provider_ref for log in logs)


async def test_a_campaign_cut_off_resumes_without_mailing_anyone_twice(
    db_session: AsyncSession, admin_user: User, factory, resend
):
    addresses = [f"yarim-{n}-{uuid.uuid4().hex[:4]}@test.com" for n in range(5)]
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    campaign = await _campaign(db_session, admin_user, addresses, status="sending", heartbeat_at=stale)
    # Two had gone out before the restart.
    db_session.add_all(
        NotificationLog(channel="email", recipient=a, purpose="campaign", campaign_id=campaign.id, provider="resend", status="sent")
        for a in addresses[:2]
    )
    await db_session.commit()
    campaign_id = campaign.id

    assert await campaigns.resume_stale(factory) == 1
    assert [m["to"][0] for m in resend["calls"][0]["json"]] == addresses[2:]
    db_session.expire_all()
    done = await db_session.get(NotificationCampaign, campaign_id)
    assert (done.status, done.sent, done.failed) == ("done", 5, 0)
    # Finished: nothing more to pick up.
    assert await campaigns.resume_stale(factory) == 0 and len(resend["calls"]) == 1


async def test_a_campaign_still_sending_is_not_taken_twice(db_session: AsyncSession, admin_user: User, factory, resend):
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    await _campaign(db_session, admin_user, ["canli@test.com"], status="sending", heartbeat_at=fresh)
    old = await _campaign(db_session, admin_user, ["eski@test.com"], status="sending", heartbeat_at=datetime.now(timezone.utc) - timedelta(days=2))
    await db_session.execute(
        update(NotificationCampaign).where(NotificationCampaign.id == old.id).values(created_at=datetime.now(timezone.utc) - timedelta(days=2))
    )
    await db_session.commit()
    # One sending right now, one cut off two days ago: neither is resumed.
    assert await campaigns.resume_stale(factory) == 0 and resend["calls"] == []


async def test_send_to_the_rest_after_failures(client: AsyncClient, db_session: AsyncSession, admin_user: User, resend):
    addresses = [f"kalan-{n}-{uuid.uuid4().hex[:4]}@test.com" for n in range(3)]
    h = auth_header(admin_user)
    # Resend refuses the whole batch (say, the day's quota): every one failed.
    resend["replies"] = [(403, {})]
    resp = await client.post(CAMPAIGNS, headers=h, json={
        "channel": "email", "audience": "manual", "manual_recipients": addresses, "consent_confirmed": True,
        "subject": "11.11", "body": "Fırsatlar başladı",
    })
    campaign_id = resp.json()["id"]
    db_session.expire_all()
    first = await db_session.get(NotificationCampaign, campaign_id)
    assert (first.status, first.sent, first.failed) == ("failed", 0, 3)

    resp = await client.post(f"{CAMPAIGNS}/{campaign_id}/resume", headers=h)
    assert resp.status_code == 202, resp.text
    db_session.expire_all()
    again = await db_session.get(NotificationCampaign, campaign_id)
    assert (again.status, again.sent, again.failed) == ("done", 3, 0)
    assert len(resend["calls"]) == 2 and json.dumps(resend["calls"][1]["json"]).count("@test.com") >= 3

    # Done: running it again sends nothing.
    assert (await client.post(f"{CAMPAIGNS}/{campaign_id}/resume", headers=h)).status_code == 202
    assert len(resend["calls"]) == 2


async def test_without_a_resend_key_mail_goes_one_by_one_with_the_headers(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, monkeypatch
):
    sent = []

    async def capture(self, to, subject, text, html, headers=None):
        sent.append((to, html, headers))
        return drivers.Delivery(True, "mock")

    monkeypatch.setattr(drivers.MockEmail, "send", capture)
    resp = await client.post(CAMPAIGNS, headers=auth_header(admin_user), json={
        "channel": "email", "audience": "manual", "manual_recipients": ["tek@test.com"], "consent_confirmed": True,
        "subject": "Merhaba", "body": "Yeni ürünler",
    })
    assert resp.status_code == 202
    (to, html, headers), = sent
    assert to == "tek@test.com" and unsubscribe.page_url(to) in html and headers == unsubscribe.headers(to)


async def test_only_admins_resume(client: AsyncClient, customer_user: User):
    assert (await client.post(f"{CAMPAIGNS}/1/resume", headers=auth_header(customer_user))).status_code == 403


#: Pass the loose "is it an address" check, but Resend refuses them (HTTP 422).
UNDELIVERABLE = [
    "kampanya99.@gmail.com",  # trailing dot in the local part: 11.11 batch 2
    ".nokta@gmail.com",
    "iki..nokta@gmail.com",
    "kuş@gmail.com",
    "ali@gmaıl.com",  # dotless ı: folds to i under IGNORECASE without ASCII
    "ali@ſite.com",
    "ali@-tire.com",
    "ali@tire-.com",
    "ali@ornek.c0m",
    "ali@ornek..com",
    "a<b>@gmail.com",
    "ali,veli@gmail.com",
    "x" * 65 + "@gmail.com",
]


@pytest.mark.parametrize("address", UNDELIVERABLE)
def test_an_address_the_provider_refuses_is_not_deliverable(address):
    assert campaigns._EMAIL.match(address)  # still counted in the campaign...
    assert not campaigns.deliverable(address)  # ...but never sent


@pytest.mark.parametrize(
    "address",
    ["ali@gmail.com", "ali.veli+11.11@ornek.com.tr", "a_b-c@alt.ornek-site.co", "x@xn--nargile-5ya.com", "o'neil@ornek.org"],
)
def test_ordinary_addresses_are_deliverable(address):
    assert campaigns.deliverable(address)


async def test_undeliverable_addresses_fail_one_by_one_and_the_batch_still_goes(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, resend
):
    good = [f"iyi-{n}-{uuid.uuid4().hex[:4]}@test.com" for n in range(4)]
    bad = UNDELIVERABLE[:3]
    h = auth_header(admin_user)
    resp = await client.post(CAMPAIGNS, headers=h, json={
        "channel": "email", "audience": "manual", "manual_recipients": [*good[:2], *bad, *good[2:]],
        "consent_confirmed": True, "subject": "11.11", "body": "Fırsatlar başladı",
    })
    assert resp.status_code == 202, resp.text

    # One batch call, with only the addresses Resend takes.
    (call,) = resend["calls"]
    assert [m["to"] for m in call["json"]] == [[a] for a in good]

    db_session.expire_all()
    campaign_id = resp.json()["id"]
    campaign = await db_session.get(NotificationCampaign, campaign_id)
    assert (campaign.status, campaign.sent, campaign.failed, campaign.total) == ("done", 4, 3, 7)
    logs = (await db_session.execute(select(NotificationLog).where(NotificationLog.campaign_id == campaign_id))).scalars().all()
    failed = {log.recipient: log for log in logs if log.status == "failed"}
    assert set(failed) == set(bad)
    assert all(log.error == campaigns.UNDELIVERABLE and log.provider == "none" for log in failed.values())

    # "Kalanlara gönder": nothing left that can go, and the bad ones are not logged again.
    assert (await client.post(f"{CAMPAIGNS}/{campaign_id}/resume", headers=h)).status_code == 202
    assert len(resend["calls"]) == 1
    count = await db_session.scalar(
        select(func.count()).select_from(NotificationLog).where(NotificationLog.campaign_id == campaign_id)
    )
    assert count == 7
    db_session.expire_all()
    again = await db_session.get(NotificationCampaign, campaign_id)
    assert (again.status, again.sent, again.failed) == ("done", 4, 3)


async def test_only_undeliverable_addresses_fail_without_calling_resend(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, resend
):
    resp = await client.post(CAMPAIGNS, headers=auth_header(admin_user), json={
        "channel": "email", "audience": "manual", "manual_recipients": ["kampanya99.@gmail.com"],
        "consent_confirmed": True, "subject": "11.11", "body": "Fırsatlar başladı",
    })
    assert resp.status_code == 202, resp.text
    assert resend["calls"] == []
    db_session.expire_all()
    campaign = await db_session.get(NotificationCampaign, resp.json()["id"])
    assert (campaign.status, campaign.sent, campaign.failed) == ("failed", 0, 1)


async def test_without_a_resend_key_undeliverable_addresses_are_not_sent_either(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, monkeypatch
):
    sent = []

    async def capture(self, to, subject, text, html, headers=None):
        sent.append(to)
        return drivers.Delivery(True, "mock")

    monkeypatch.setattr(drivers.MockEmail, "send", capture)
    resp = await client.post(CAMPAIGNS, headers=auth_header(admin_user), json={
        "channel": "email", "audience": "manual", "manual_recipients": ["tek@test.com", "kuş@gmail.com"],
        "consent_confirmed": True, "subject": "Merhaba", "body": "Yeni ürünler",
    })
    assert resp.status_code == 202
    assert sent == ["tek@test.com"]
    db_session.expire_all()
    campaign = await db_session.get(NotificationCampaign, resp.json()["id"])
    assert (campaign.sent, campaign.failed) == (1, 1)
