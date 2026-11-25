""""Kalıcı Olarak Sil" on a customer's page: a customer without orders is
deleted with everything that was only theirs; one with orders is emptied
instead — personal details erased, login closed — and the orders, the reports
and every other account stay as they were. Admin and staff accounts cannot be
deleted here."""

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access import CustomerAccessRule
from app.models.loyalty import LoyaltyLedger
from app.models.order import Order
from app.models.user import CustomerAddress, CustomerProfile, SegmentType, User, UserRole
from tests.conftest import ADDRESS, CONSENTS, auth_header
from tests.test_b2b_havale import _firm

URL = "/api/v1/admin/customers/{}"


async def _count(db: AsyncSession, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    return await db.scalar(stmt.execution_options(populate_existing=True))


async def _me(client: AsyncClient, headers: dict) -> dict:
    return (await client.get("/api/v1/auth/me", headers=headers)).json()


async def test_a_customer_without_orders_is_deleted_with_what_was_only_theirs(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User, customer_user: User
):
    firm = await _firm(client, db_session, segments, SegmentType.RETAIL)  # with a company profile and an address
    user_id = uuid.UUID((await _me(client, firm))["id"])
    h = auth_header(admin_user)
    other_id = customer_user.id
    users_before = await _count(db_session, User)
    # Some of everything an account can have of its own.
    assert (await client.post(f"{URL.format(user_id)}/points", headers=h, json={"amount": 100, "note": "deneme"})).status_code == 200
    assert (await client.put(f"/api/v1/admin/access-rules/customers/{user_id}", headers=h, json={"routes": ["/blog"]})).status_code == 200
    assert await _count(db_session, CustomerProfile, user_id=user_id) == 1
    assert await _count(db_session, CustomerAddress, user_id=user_id) >= 1

    resp = await client.delete(URL.format(user_id), headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mode": "deleted", "orders_kept": 0}
    for model in (CustomerProfile, CustomerAddress, CustomerAccessRule, LoyaltyLedger):
        assert await _count(db_session, model, user_id=user_id) == 0, model.__name__
    assert await _count(db_session, User, id=user_id) == 0
    assert await _count(db_session, User) == users_before - 1
    assert await _count(db_session, User, id=other_id) == 1  # nobody else
    assert (await client.get(URL.format(user_id), headers=h)).status_code == 404
    # Its session is gone with it.
    assert (await client.get("/api/v1/auth/me", headers=firm)).status_code == 401


async def test_a_customer_with_orders_is_emptied_and_the_orders_stay(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    user_id = customer_user.id
    email = customer_user.email
    own = auth_header(customer_user)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=own,
        json={**CONSENTS, "payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(normal_v.id), "quantity": 1}],
              "shipping_address": ADDRESS, "shipping_consent": True},
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    h = auth_header(admin_user)
    summary_before = (await client.get("/api/v1/admin/reports/summary", headers=h)).json()

    resp = await client.delete(URL.format(user_id), headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mode": "anonymized", "orders_kept": 1}

    kept = await db_session.get(Order, order["id"], populate_existing=True)
    assert kept is not None and kept.user_id == user_id and kept.total_amount == kept.total_amount
    assert kept.shipping_address["full_name"] == ADDRESS["full_name"]  # the order's own snapshot stands
    user = await db_session.get(User, user_id, populate_existing=True)
    assert user.email != email and user.email.endswith("@silinmis.invalid")
    assert (user.full_name, user.phone, user.username, user.is_active) == ("Silinmiş müşteri", None, None, False)
    assert await _count(db_session, CustomerAddress, user_id=user_id) == 0
    # Signed out, and cannot sign in again.
    assert (await client.get("/api/v1/auth/me", headers=own)).status_code == 401
    # The order is still on the admin's list, and the reports did not move.
    listed = (await client.get("/api/v1/admin/orders", headers=h, params={"number": order["order_number"]})).json()
    assert [o["id"] for o in listed] == [order["id"]]
    assert (await client.get("/api/v1/admin/reports/summary", headers=h)).json() == summary_before
    # Asked again: still a customer with orders, emptied once more without error.
    again = await client.delete(URL.format(user_id), headers=h)
    assert again.status_code == 200 and again.json()["mode"] == "anonymized"


async def test_admins_and_staff_are_not_deleted_here_and_only_admins_delete(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User
):
    h = auth_header(admin_user)
    staff = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x", role=UserRole.STAFF, is_active=True, is_verified=True,
    )
    db_session.add(staff)
    await db_session.commit()
    staff_id, admin_id, customer_id = staff.id, admin_user.id, customer_user.id
    assert (await client.delete(URL.format(staff_id), headers=h)).status_code == 403
    assert (await client.delete(URL.format(admin_id), headers=h)).status_code == 403
    assert (await client.delete(URL.format(customer_id), headers=auth_header(customer_user))).status_code == 403
    assert (await client.delete(URL.format(uuid.uuid4()), headers=h)).status_code == 404
    assert await _count(db_session, User, id=customer_id) == 1
