"""Üye olmadan alışveriş: a guest checkout, its isolation, and turning it into an account."""

import random
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import SegmentType, User, UserRole
from app.services import password_reset
from app.services.contact_phone import mobile_taken
from tests.conftest import auth_header
from tests.test_order_note import _body, _variant

pytestmark = pytest.mark.asyncio

GUEST = "/api/v1/auth/guest"
CLAIM = "/api/v1/auth/guest/claim"
ME = "/api/v1/auth/me"


def _email() -> str:
    return f"misafir-{uuid.uuid4().hex[:8]}@test.com"


def _mobile() -> str:
    return f"0534 {random.randint(1000000, 9999999)}"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _guest(client: AsyncClient, email: str | None = None, phone: str | None = None) -> tuple[str, str]:
    email = email or _email()
    resp = await client.post(GUEST, json={"email": email, "full_name": "  Misafir   Alıcı ", "phone": phone or _mobile()})
    assert resp.status_code == 201, resp.text
    return email, resp.json()["access_token"]


async def _account(db: AsyncSession, email: str, segments=None, phone: str | None = None) -> User:
    user = User(
        email=email,
        hashed_password=hash_password("hesap-sifre-1"),
        full_name="Kayıtlı Müşteri",
        phone=phone,
        role=UserRole.CUSTOMER,
        segment_id=segments[SegmentType.RETAIL].id if segments else None,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_a_guest_session_is_a_retail_buyer_never_asked_again(client: AsyncClient, segments):
    _, token = await _guest(client)
    me = (await client.get(ME, headers=_bearer(token))).json()
    assert (me["is_guest"], me["full_name"], me["segment_code"], me["account_type"]) == (True, "Misafir Alıcı", "RETAIL", "INDIVIDUAL")
    # The checkout's own box asks for campaign consent.
    assert me["consent_prompt_due"] is False


@pytest.mark.parametrize("body", [{"email": "yok", "full_name": "Ali Veli", "phone": "0532 111 22 33"}, {"email": "a@b.com", "full_name": "A", "phone": "0532 111 22 33"}, {"email": "a@b.com", "full_name": "Ali Veli", "phone": "12"}])
async def test_a_guest_needs_an_address_a_name_and_a_number(client: AsyncClient, body):
    assert (await client.post(GUEST, json=body)).status_code == 422


async def test_an_address_with_an_account_signs_in_instead(client: AsyncClient, db_session: AsyncSession):
    email = _email()
    await _account(db_session, email)
    resp = await client.post(GUEST, json={"email": email.upper(), "full_name": "Ali Veli", "phone": _mobile()})
    assert resp.status_code == 409
    assert "giriş" in resp.json()["detail"]


async def test_a_guest_orders_and_another_guest_with_the_same_address_cannot_see_it(client: AsyncClient, db_session: AsyncSession, segments):
    email, first = await _guest(client)
    variant = await _variant(db_session, segments)
    placed = await client.post("/api/v1/orders/checkout", headers=_bearer(first), json={**_body(variant), "save_card": True})
    assert placed.status_code == 201, placed.text
    order = placed.json()

    _, second = await _guest(client, email=email)
    assert (await client.get("/api/v1/orders", headers=_bearer(second))).json() == []
    assert (await client.get(f"/api/v1/orders/{order['id']}", headers=_bearer(second))).status_code == 404
    assert [o["id"] for o in (await client.get("/api/v1/orders", headers=_bearer(first))).json()] == [order["id"]]


async def test_a_guest_cannot_sign_in_and_does_not_hold_its_address(client: AsyncClient):
    email, _ = await _guest(client)
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": "!guest"})).status_code == 401
    # The address is still free for an account of its own.
    created = await client.post(
        "/api/v1/auth/register",
        json={
            "account_type": "INDIVIDUAL",
            "email": email,
            "password": "Str0ng-Passw0rd",
            "full_name": "Yeni Müşteri",
            "phone": _mobile(),
            "billing_address": {"city": "Ankara", "district": "Çankaya", "street": "Atatürk Blv.", "building_no": "1"},
        },
    )
    assert created.status_code == 201, created.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd"})
    assert login.status_code == 200
    me = (await client.get(ME, headers=_bearer(login.json()["access_token"]))).json()
    assert me["is_guest"] is False


async def test_a_guest_becomes_an_account_with_its_orders(client: AsyncClient, db_session: AsyncSession, segments):
    email, token = await _guest(client)
    variant = await _variant(db_session, segments)
    order = (await client.post("/api/v1/orders/checkout", headers=_bearer(token), json=_body(variant))).json()

    assert (await client.post(CLAIM, headers=_bearer(token), json={"password": "kisa"})).status_code == 422
    claimed = await client.post(CLAIM, headers=_bearer(token), json={"password": "yeni-sifre-1"})
    assert claimed.status_code == 200, claimed.text
    # The guest session ends; the account's begins.
    assert (await client.get(ME, headers=_bearer(token))).status_code == 401
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "yeni-sifre-1"})
    assert login.status_code == 200
    account = _bearer(login.json()["access_token"])
    assert (await client.get(ME, headers=account)).json()["is_guest"] is False
    assert [o["id"] for o in (await client.get("/api/v1/orders", headers=account)).json()] == [order["id"]]
    # Claimed once: it is an account now.
    assert (await client.post(CLAIM, headers=account, json={"password": "baska-sifre-1"})).status_code == 409


async def test_a_guest_cannot_claim_an_address_an_account_has(client: AsyncClient, db_session: AsyncSession):
    email, token = await _guest(client)
    await _account(db_session, email)
    assert (await client.post(CLAIM, headers=_bearer(token), json={"password": "yeni-sifre-1"})).status_code == 409


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/api/v1/me/profile", {"full_name": "Ali Veli", "phone": "0532 111 22 33"}),
        ("post", "/api/v1/me/password", {"current_password": "x", "new_password": "yeni-sifre-1"}),
        ("post", "/api/v1/me/address-book", {"city": "İstanbul", "district": "Kadıköy", "line": "Moda Cad. No: 5"}),
    ],
)
async def test_what_only_an_account_keeps_is_closed_to_a_guest(client: AsyncClient, method, path, body):
    _, token = await _guest(client)
    resp = await client.request(method.upper(), path, headers=_bearer(token), json=body)
    assert resp.status_code == 403
    assert "hesap" in resp.json()["detail"].lower()


async def test_kargo_takip_finds_an_order_by_its_number_and_address(client: AsyncClient, db_session: AsyncSession, segments):
    email, token = await _guest(client)
    variant = await _variant(db_session, segments)
    order = (await client.post("/api/v1/orders/checkout", headers=_bearer(token), json=_body(variant))).json()

    found = await client.get("/api/v1/orders/lookup", params={"order_number": order["order_number"].lower(), "email": email.upper()})
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["order_number"] == order["order_number"]
    # Where the order stands, not who placed it or where it goes.
    assert "shipping_address" not in body and "user_id" not in body
    assert (await client.get("/api/v1/orders/lookup", params={"order_number": order["order_number"], "email": "baska@test.com"})).status_code == 404


async def test_a_reset_and_a_phone_check_never_reach_a_guest(client: AsyncClient, db_session: AsyncSession):
    phone = _mobile()
    email, _ = await _guest(client, phone=phone)
    assert await password_reset.find_by_email(db_session, email) is None
    assert await password_reset.find_by_phone(db_session, phone) is None
    # An account may take a number a guest ordered with, and SMS still finds it.
    account = await _account(db_session, _email(), phone=phone)
    assert not await mobile_taken(db_session, phone, account.id)
    assert (await password_reset.find_by_phone(db_session, phone)).id == account.id
    guests = (await db_session.scalars(select(User).where(User.email == email))).all()
    assert [g.is_guest for g in guests] == [True]
