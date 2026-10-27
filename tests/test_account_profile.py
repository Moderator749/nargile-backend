"""Hesabım: the customer's name and number, their password, their addresses —
and the number an order is placed with landing on an account that had none.
"""

import random
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, opencart_hash, verify_password
from app.models.user import AccountType, AddressKind, CustomerAddress, SHIPPING_ADDRESS_LIMIT, User, UserRole
from tests.conftest import auth_header
from tests.test_order_note import _body, _variant

pytestmark = pytest.mark.asyncio

PROFILE = "/api/v1/me/profile"
PASSWORD = "/api/v1/me/password"
BOOK = "/api/v1/me/address-book"
HOME = {"label": "Ev", "city": "istanbul", "district": "Kadıköy", "line": "Moda Cad. No: 5 D: 3"}


def _mobile() -> str:
    """A Turkish mobile no other test's account has."""
    return f"0533 {random.randint(1000000, 9999999)}"


async def _customer(
    db: AsyncSession, *, phone: str | None = None, account_type: AccountType = AccountType.INDIVIDUAL, password: str = "eski-sifre-1"
) -> User:
    user = User(
        email=f"hesap-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password=hash_password(password),
        full_name="Ayşe Kaya",
        phone=phone,
        role=UserRole.CUSTOMER,
        account_type=account_type,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ── Hesap bilgilerim ─────────────────────────────────────────────────────────


async def test_the_name_and_number_are_saved(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    number = _mobile()
    resp = await client.put(PROFILE, headers=auth_header(user), json={"full_name": "  Ayşe   Yılmaz ", "phone": number})
    assert resp.status_code == 200, resp.text
    me = (await client.get("/api/v1/auth/me", headers=auth_header(user))).json()
    assert (me["full_name"], me["phone"]) == ("Ayşe Yılmaz", number)


@pytest.mark.parametrize("body", [{"full_name": "A", "phone": "0532 123 45 67"}, {"full_name": "Ayşe Kaya", "phone": "12"}])
async def test_a_name_or_number_nobody_could_use_is_refused(client: AsyncClient, db_session: AsyncSession, body):
    user = await _customer(db_session)
    assert (await client.put(PROFILE, headers=auth_header(user), json=body)).status_code == 422


async def test_a_mobile_on_another_account_is_refused(client: AsyncClient, db_session: AsyncSession):
    number = _mobile()
    await _customer(db_session, phone=number)
    user = await _customer(db_session)
    # Written another way, it is still the same number.
    resp = await client.put(PROFILE, headers=auth_header(user), json={"full_name": "Ayşe Kaya", "phone": "+90" + number[1:]})
    assert resp.status_code == 409
    await db_session.refresh(user)
    assert user.phone is None


# ── Şifre ────────────────────────────────────────────────────────────────────


async def test_the_password_changes_with_the_current_one(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    old_session = auth_header(user)

    wrong = await client.post(PASSWORD, headers=old_session, json={"current_password": "yanlis", "new_password": "yeni-sifre-1"})
    assert wrong.status_code == 400
    short = await client.post(PASSWORD, headers=old_session, json={"current_password": "eski-sifre-1", "new_password": "kisa"})
    assert short.status_code == 422

    resp = await client.post(PASSWORD, headers=old_session, json={"current_password": "eski-sifre-1", "new_password": "yeni-sifre-1"})
    assert resp.status_code == 200, resp.text
    await db_session.refresh(user)
    assert verify_password("yeni-sifre-1", user.hashed_password)
    # Other sessions end; this one carries on with the token it was given.
    assert (await client.get("/api/v1/auth/me", headers=old_session)).status_code == 401
    fresh = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert (await client.get("/api/v1/auth/me", headers=fresh)).status_code == 200
    login = await client.post("/api/v1/auth/login", json={"email": user.email, "password": "yeni-sifre-1"})
    assert login.status_code == 200


async def test_an_opencart_customer_changes_the_old_password(client: AsyncClient, db_session: AsyncSession):
    import hashlib

    sha1 = lambda s: hashlib.sha1(s.encode()).hexdigest()  # noqa: E731
    user = await _customer(db_session)
    user.hashed_password = opencart_hash(sha1("salt" + sha1("salt" + sha1("eski-oc"))), "salt")
    await db_session.commit()
    resp = await client.post(PASSWORD, headers=auth_header(user), json={"current_password": "eski-oc", "new_password": "yeni-sifre-1"})
    assert resp.status_code == 200, resp.text
    await db_session.refresh(user)
    assert user.hashed_password.startswith("$2b$")


# ── Adreslerim ───────────────────────────────────────────────────────────────


async def test_delivery_addresses_are_added_edited_and_deleted(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session, phone=_mobile())
    db_session.add(CustomerAddress(user_id=user.id, kind=AddressKind.BILLING, city="Ankara", district="Çankaya", street="Atatürk Blv. 1"))
    await db_session.commit()
    headers = auth_header(user)

    added = await client.post(BOOK, headers=headers, json=HOME)
    assert added.status_code == 201, added.text
    home = added.json()
    assert (home["kind"], home["label"], home["city"], home["editable"], home["deletable"]) == (
        "SHIPPING", "Ev", "İstanbul", True, True,
    )
    work = (await client.post(BOOK, headers=headers, json={**HOME, "label": None, "district": "Şişli", "line": "Halaskargazi Cad. 10"})).json()
    assert work["label"] == "Teslimat Adresi"

    edited = await client.put(f"{BOOK}/{home['id']}", headers=headers, json={**HOME, "label": "Annem", "line": "Moda Cad. No: 7"})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["label"], edited.json()["line"]) == ("Annem", "Moda Cad. No: 7")

    book = (await client.get(BOOK, headers=headers)).json()
    assert [(a["kind"], a["label"]) for a in book] == [("BILLING", "Fatura Adresi"), ("SHIPPING", "Annem"), ("SHIPPING", "Teslimat Adresi")]
    billing = book[0]
    assert (billing["editable"], billing["deletable"]) == (True, False)

    # Checkout offers the same addresses, in the same order.
    offered = (await client.get("/api/v1/me/addresses", headers=headers)).json()
    assert [a["id"] for a in offered] == [a["id"] for a in book]

    assert (await client.delete(f"{BOOK}/{billing['id']}", headers=headers)).status_code == 403
    assert (await client.delete(f"{BOOK}/{work['id']}", headers=headers)).status_code == 204
    assert [a["id"] for a in (await client.get(BOOK, headers=headers)).json()] == [billing["id"], home["id"]]

    # Their own invoice address is corrected here too.
    fixed = await client.put(f"{BOOK}/{billing['id']}", headers=headers, json={"city": "Ankara", "district": "Yenimahalle", "line": "Ragıp Tüzün Cad. 2"})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["district"] == "Yenimahalle"


async def test_an_address_abroad_keeps_its_own_shape(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    resp = await client.post(
        BOOK, headers=auth_header(user),
        json={"label": "Berlin", "country": "de", "city": "Berlin", "district": "Mitte", "postal_code": "10115", "line": "Unter den Linden 1"},
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert (entry["country"], entry["district"], entry["postal_code"]) == ("DE", None, "10115")
    row = await db_session.get(CustomerAddress, entry["id"])
    await db_session.refresh(row)
    assert (row.address_line1, row.street) == ("Unter den Linden 1", None)


@pytest.mark.parametrize(
    "body", [{**HOME, "city": "Narnia"}, {**HOME, "district": None}, {**HOME, "line": "kısa"}]
)
async def test_an_address_that_could_not_be_shipped_to_is_refused(client: AsyncClient, db_session: AsyncSession, body):
    user = await _customer(db_session)
    assert (await client.post(BOOK, headers=auth_header(user), json=body)).status_code == 422


async def test_someone_elses_address_is_not_found(client: AsyncClient, db_session: AsyncSession):
    owner = await _customer(db_session)
    entry = (await client.post(BOOK, headers=auth_header(owner), json=HOME)).json()
    other = await _customer(db_session)
    assert (await client.put(f"{BOOK}/{entry['id']}", headers=auth_header(other), json=HOME)).status_code == 404
    assert (await client.delete(f"{BOOK}/{entry['id']}", headers=auth_header(other))).status_code == 404


async def test_the_book_has_a_limit(client: AsyncClient, db_session: AsyncSession):
    user = await _customer(db_session)
    for _ in range(SHIPPING_ADDRESS_LIMIT):
        assert (await client.post(BOOK, headers=auth_header(user), json=HOME)).status_code == 201
    assert (await client.post(BOOK, headers=auth_header(user), json=HOME)).status_code == 409


async def test_a_firm_ships_to_its_registered_address(client: AsyncClient, db_session: AsyncSession):
    firm = await _customer(db_session, account_type=AccountType.CAFE)
    db_session.add(CustomerAddress(user_id=firm.id, kind=AddressKind.BILLING, city="Ankara", district="Çankaya", street="Atatürk Blv. 1"))
    await db_session.commit()
    assert (await client.post(BOOK, headers=auth_header(firm), json=HOME)).status_code == 403
    [registered] = (await client.get(BOOK, headers=auth_header(firm))).json()
    assert (registered["label"], registered["editable"], registered["deletable"]) == ("Kurumsal Fatura Adresi", False, False)
    assert (await client.put(f"{BOOK}/{registered['id']}", headers=auth_header(firm), json=HOME)).status_code == 403


# ── Checkout ─────────────────────────────────────────────────────────────────


async def test_an_order_puts_its_number_on_an_account_without_one(client: AsyncClient, db_session: AsyncSession, segments):
    from app.models.user import SegmentType

    user = await _customer(db_session)
    user.segment_id = segments[SegmentType.RETAIL].id
    await db_session.commit()
    variant = await _variant(db_session, segments)
    number = _mobile()
    body = _body(variant)
    body["shipping_address"] = {**body["shipping_address"], "phone": number}

    placed = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_address"]["phone"] == number
    await db_session.refresh(user)
    assert user.phone == number


async def test_an_order_does_not_rewrite_a_number_on_file(client: AsyncClient, db_session: AsyncSession, segments):
    from app.models.user import SegmentType

    own = _mobile()
    user = await _customer(db_session, phone=own)
    user.segment_id = segments[SegmentType.RETAIL].id
    await db_session.commit()
    variant = await _variant(db_session, segments)
    body = _body(variant)
    body["shipping_address"] = {**body["shipping_address"], "phone": _mobile()}

    assert (await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)).status_code == 201
    await db_session.refresh(user)
    assert user.phone == own


async def test_an_order_does_not_take_another_accounts_number(client: AsyncClient, db_session: AsyncSession, segments):
    from app.models.user import SegmentType

    taken = _mobile()
    await _customer(db_session, phone=taken)
    user = await _customer(db_session)
    user.segment_id = segments[SegmentType.RETAIL].id
    await db_session.commit()
    variant = await _variant(db_session, segments)
    body = _body(variant)
    body["shipping_address"] = {**body["shipping_address"], "phone": taken}

    assert (await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)).status_code == 201
    assert (await db_session.scalar(select(User.phone).where(User.id == user.id))) is None
