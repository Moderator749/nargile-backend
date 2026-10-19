"""Sessions: how long a login lasts, renewing it, and ending every one at a password reset."""

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import password_reset as reset_api
from app.core.config import settings
from app.core.security import create_access_token, create_session_token, hash_password
from app.models.user import User, UserRole
from app.services import password_reset
from tests.conftest import auth_header

ME = "/api/v1/auth/me"
REFRESH = "/api/v1/auth/refresh"


def _claims(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


def _lifetime(token: str) -> timedelta:
    claims = _claims(token)
    return timedelta(seconds=claims["exp"] - claims["iat"])


async def _login(client: AsyncClient, db: AsyncSession, user: User, password: str = "gizli-sifre-1") -> str:
    user.hashed_password = hash_password(password)
    await db.commit()
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_a_customer_stays_signed_in_for_thirty_days(client: AsyncClient, db_session: AsyncSession, customer_user: User):
    token = await _login(client, db_session, customer_user)
    assert _lifetime(token) == timedelta(days=settings.CUSTOMER_SESSION_DAYS) == timedelta(days=30)
    assert _claims(token)["ver"] == 0


async def test_the_panel_staff_get_a_day(client: AsyncClient, db_session: AsyncSession, admin_user: User):
    token = await _login(client, db_session, admin_user)
    assert _lifetime(token) == timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES) == timedelta(hours=24)
    staff = User(email="staff-session@test.com", hashed_password="x", role=UserRole.STAFF, is_active=True)
    assert _lifetime(create_session_token(staff)) == timedelta(hours=24)


async def test_a_live_session_is_renewed_and_a_dead_one_is_not(
    client: AsyncClient, customer_user: User
):
    renewed = await client.post(REFRESH, headers=auth_header(customer_user))
    assert renewed.status_code == 200, renewed.text
    fresh = renewed.json()["access_token"]
    assert _lifetime(fresh) == timedelta(days=30)
    assert (await client.get(ME, headers={"Authorization": f"Bearer {fresh}"})).status_code == 200

    expired = create_access_token({"sub": str(customer_user.id), "role": "CUSTOMER"}, expires_delta=timedelta(seconds=-1))
    assert (await client.post(REFRESH, headers={"Authorization": f"Bearer {expired}"})).status_code == 401
    assert (await client.post(REFRESH)).status_code in (401, 403)  # no token at all


async def test_a_password_reset_ends_every_earlier_session(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, monkeypatch
):
    before = await _login(client, db_session, customer_user)
    assert (await client.get(ME, headers={"Authorization": f"Bearer {before}"})).status_code == 200

    reset_api._hits.clear()
    monkeypatch.setattr(password_reset.secrets, "token_urlsafe", lambda n: "session-reset-" + customer_user.email)
    assert (
        await client.post("/api/v1/auth/password-reset/request", json={"channel": "email", "identifier": customer_user.email})
    ).status_code == 202
    done = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "session-reset-" + customer_user.email, "new_password": "yeni-sifre-123"},
    )
    assert done.status_code == 200, done.text
    reset_api._hits.clear()

    # The old session is over, and cannot be renewed into a new one.
    old = {"Authorization": f"Bearer {before}"}
    assert (await client.get(ME, headers=old)).status_code == 401
    assert (await client.post(REFRESH, headers=old)).status_code == 401
    # Signing in with the new password works, at the new version.
    resp = await client.post("/api/v1/auth/login", json={"email": customer_user.email, "password": "yeni-sifre-123"})
    after = resp.json()["access_token"]
    assert _claims(after)["ver"] == 1
    assert (await client.get(ME, headers={"Authorization": f"Bearer {after}"})).status_code == 200


async def test_a_token_from_before_versions_still_works(client: AsyncClient, customer_user: User):
    """Tokens issued before this change carry no version: they count as 0, so nobody is signed out by the deploy."""
    legacy = create_access_token({"sub": str(customer_user.id), "role": "CUSTOMER"})
    assert "ver" not in _claims(legacy)
    assert (await client.get(ME, headers={"Authorization": f"Bearer {legacy}"})).status_code == 200


async def test_a_deactivated_account_is_out_at_once(client: AsyncClient, db_session: AsyncSession, customer_user: User):
    token = await _login(client, db_session, customer_user)
    customer_user.is_active = False
    await db_session.commit()
    assert (await client.post(REFRESH, headers={"Authorization": f"Bearer {token}"})).status_code == 401


def test_issued_at_is_now():
    claims = _claims(create_access_token({"sub": "x"}))
    assert abs(claims["iat"] - datetime.now(timezone.utc).timestamp()) < 5
