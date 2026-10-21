"""Six characters for a customer's password, eight for an admin's, and failed
logins counted per account name. Also: money written to customers as "TL"."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import auth as auth_api
from app.core.money import format_tl
from app.core.security import hash_password, verify_password
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services import password_reset

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REQUEST = "/api/v1/auth/password-reset/request"
CONFIRM = "/api/v1/auth/password-reset/confirm"


def _individual(password: str) -> dict:
    return {
        "account_type": "INDIVIDUAL",
        "email": f"pw-{uuid.uuid4().hex[:8]}@test.com",
        "password": password,
        "full_name": "Ayşe Yılmaz",
        "phone": "0532 111 22 33",
    }


async def _user(db: AsyncSession, role: UserRole) -> User:
    seg_id = await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))
    user = User(
        email=f"pw-{uuid.uuid4().hex[:8]}@test.com", hashed_password=hash_password("eski-sifre-1"),
        role=role, segment_id=seg_id, is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _reset_link(client: AsyncClient, user: User, monkeypatch) -> str:
    monkeypatch.setattr(password_reset.secrets, "token_urlsafe", lambda n: "policy-" + user.email)
    assert (await client.post(REQUEST, json={"channel": "email", "identifier": user.email})).status_code == 202
    return "policy-" + user.email


async def test_a_customer_registers_with_six_characters(client: AsyncClient):
    assert (await client.post(REGISTER, json=_individual("abc123"))).status_code == 201
    assert (await client.post(REGISTER, json=_individual("abc12"))).status_code == 422


async def test_a_customer_resets_to_six_characters_an_admin_does_not(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    customer = await _user(db_session, UserRole.CUSTOMER)
    token = await _reset_link(client, customer, monkeypatch)
    assert (await client.post(CONFIRM, json={"token": token, "new_password": "kisa12"})).status_code == 200
    await db_session.refresh(customer)
    assert verify_password("kisa12", customer.hashed_password)

    admin = await _user(db_session, UserRole.ADMIN)
    token = await _reset_link(client, admin, monkeypatch)
    short = await client.post(CONFIRM, json={"token": token, "new_password": "kisa12"})
    assert short.status_code == 422 and "en az 8" in short.json()["detail"]
    # The link is not spent by the refusal: a long enough password goes through.
    ok = await client.post(CONFIRM, json={"token": token, "new_password": "uzun-sifre-1"})
    assert ok.status_code == 200, ok.text
    await db_session.refresh(admin)
    assert verify_password("uzun-sifre-1", admin.hashed_password)


async def test_failed_logins_are_limited_per_account_name(client: AsyncClient, db_session: AsyncSession):
    user = await _user(db_session, UserRole.CUSTOMER)
    wrong = {"email": user.email, "password": "yanlis-1"}
    for _ in range(auth_api.LOGIN_FAILURES - 1):
        assert (await client.post(LOGIN, json=wrong)).status_code == 401
    # A success before the limit clears the count.
    assert (await client.post(LOGIN, json={"email": user.email.upper(), "password": "eski-sifre-1"})).status_code == 200
    for _ in range(auth_api.LOGIN_FAILURES):
        assert (await client.post(LOGIN, json=wrong)).status_code == 401
    # Locked now, even with the right password; another account is not.
    locked = await client.post(LOGIN, json={"email": user.email, "password": "eski-sifre-1"})
    assert locked.status_code == 429
    other = await _user(db_session, UserRole.CUSTOMER)
    assert (await client.post(LOGIN, json={"email": other.email, "password": "eski-sifre-1"})).status_code == 200


def test_money_is_written_with_tl_after_it():
    assert format_tl("6700") == "6.700,00 TL"
    assert format_tl("1234567.5") == "1.234.567,50 TL"
    assert format_tl(0) == "0,00 TL"
