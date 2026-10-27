"""Ticari elektronik ileti onayı from checkout's box and the one-time card.

Both only give consent, and only when the customer ticks it; nothing here
withdraws one, which is the preferences switches' job. The card asks an
account that was never asked — the ones the old store brought over — once.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import SegmentType, User, UserRole
from tests.conftest import auth_header
from tests.test_order_note import _body, _variant

pytestmark = pytest.mark.asyncio

ME = "/api/v1/auth/me"
PROMPT = "/api/v1/me/consent-prompt"


async def _customer(db: AsyncSession, segments=None, **fields) -> User:
    user = User(
        email=f"izin-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        full_name="İzin Müşteri",
        role=fields.pop("role", UserRole.CUSTOMER),
        segment_id=segments[SegmentType.RETAIL].id if segments else None,
        is_active=True,
        **fields,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _me(client: AsyncClient, user: User) -> dict:
    return (await client.get(ME, headers=auth_header(user))).json()


async def test_an_account_never_asked_is_asked_once(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    me = await _me(client, user)
    assert (me["consent_prompt_due"], me["marketing_email_consent"], me["marketing_sms_consent"]) == (True, False, False)

    answered = await client.post(PROMPT, headers=auth_header(user), json={"email": True, "sms": False})
    assert answered.json() == {"email": True, "sms": False}
    await db_session.refresh(user)
    assert user.marketing_email_consent_at is not None and user.marketing_sms_consent_at is None
    me = await _me(client, user)
    assert (me["consent_prompt_due"], me["marketing_email_consent"]) == (False, True)


async def test_not_now_withdraws_nothing_and_stops_asking(client: AsyncClient, db_session: AsyncSession):
    given = datetime(2020, 1, 1, tzinfo=timezone.utc)
    user = await _customer(db_session, marketing_email_consent_at=given)
    assert (await _me(client, user))["consent_prompt_due"] is True

    resp = await client.post(PROMPT, headers=auth_header(user), json={"email": False, "sms": False})
    assert resp.json() == {"email": True, "sms": False}
    await db_session.refresh(user)
    # The consent keeps the moment it was first given.
    assert user.marketing_email_consent_at == given
    assert (await _me(client, user))["consent_prompt_due"] is False


async def test_nobody_is_asked_twice(client: AsyncClient, db_session: AsyncSession):
    both = await _customer(
        db_session,
        marketing_email_consent_at=datetime.now(timezone.utc),
        marketing_sms_consent_at=datetime.now(timezone.utc),
    )
    assert (await _me(client, both))["consent_prompt_due"] is False
    staff = await _customer(db_session, role=UserRole.ADMIN)
    assert (await _me(client, staff))["consent_prompt_due"] is False

    switched = await _customer(db_session)
    await client.put("/api/v1/me/communication-preferences", headers=auth_header(switched), json={"email": False, "sms": True})
    assert (await _me(client, switched))["consent_prompt_due"] is False


async def test_a_sign_up_was_asked_on_the_form(client: AsyncClient):
    email = f"kayit-{uuid.uuid4().hex[:8]}@test.com"
    created = await client.post(
        "/api/v1/auth/register",
        json={
            "account_type": "INDIVIDUAL",
            "email": email,
            "password": "Str0ng-Passw0rd",
            "full_name": "Yeni Müşteri",
            "phone": "0532 987 65 43",
            "billing_address": {"city": "Ankara", "district": "Çankaya", "street": "Atatürk Blv.", "building_no": "1"},
        },
    )
    assert created.status_code in (200, 201), created.text
    token = (await client.post("/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd"})).json()["access_token"]
    me = (await client.get(ME, headers={"Authorization": f"Bearer {token}"})).json()
    assert (me["consent_prompt_due"], me["marketing_email_consent"]) == (False, False)


async def test_the_checkout_box_records_both_consents(client: AsyncClient, db_session: AsyncSession, segments):
    user = await _customer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=_body(variant, marketing_consent=True))
    assert placed.status_code == 201, placed.text
    await db_session.refresh(user)
    assert user.marketing_email_consent_at is not None and user.marketing_sms_consent_at is not None
    assert (await _me(client, user))["consent_prompt_due"] is False


async def test_an_unticked_box_changes_nothing(client: AsyncClient, db_session: AsyncSession, segments):
    given = datetime(2021, 5, 5, tzinfo=timezone.utc)
    user = await _customer(db_session, segments, marketing_sms_consent_at=given)
    variant = await _variant(db_session, segments)
    placed = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=_body(variant))
    assert placed.status_code == 201, placed.text
    await db_session.refresh(user)
    assert (user.marketing_email_consent_at, user.marketing_sms_consent_at, user.consent_prompted_at) == (None, given, None)
