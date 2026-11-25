""""Stok ve Raporlardan Hariç Tut (Şube / Test Hesabı)": a branch's or test
account's orders take no online stock (and may be placed when it has none),
put none back, earn no points, stay out of the dashboard, the reports and the
bestsellers, send no order SMS — and stay listed, with a badge and a filter."""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory
from app.models.order import Order, OrderItem
from app.models.user import User
from app.services import sms_settings
from tests.conftest import ADDRESS, CONSENTS, auth_header
from tests.test_order_sms import _logs, no_saved_sms_settings  # noqa: F401  (fixture)


async def _stock(db: AsyncSession, variant_id) -> int:
    rows = await db.scalars(
        select(Inventory).where(Inventory.variant_id == variant_id).execution_options(populate_existing=True)
    )
    return sum(r.quantity for r in rows.all())


async def _set_stock(db: AsyncSession, variant_id, quantity: int) -> None:
    await db.execute(update(Inventory).where(Inventory.variant_id == variant_id).values(quantity=quantity))
    await db.commit()


async def _branch(client: AsyncClient, admin: User, user: User, enabled: bool = True) -> dict:
    resp = await client.put(
        f"/api/v1/admin/customers/{user.id}/stock-exclusion", headers=auth_header(admin), json={"enabled": enabled}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _checkout(client: AsyncClient, user: User, variant_id, quantity: int):
    return await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user),
        json={**CONSENTS, "payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(variant_id), "quantity": quantity}],
              "shipping_address": ADDRESS, "shipping_consent": True},
    )


async def test_the_flag_is_set_from_the_panel_and_shown_everywhere(
    client: AsyncClient, admin_user: User, customer_user: User
):
    detail = await _branch(client, admin_user, customer_user)
    assert detail["exclude_from_stock_and_reports"] is True
    grid = (await client.get("/api/v1/admin/customers", headers=auth_header(admin_user), params={"q": customer_user.email})).json()
    assert [c["exclude_from_stock_and_reports"] for c in grid if c["id"] == str(customer_user.id)] == [True]
    me = (await client.get("/api/v1/auth/me", headers=auth_header(customer_user))).json()
    assert me["exclude_from_stock_and_reports"] is True
    assert (await client.put(
        f"/api/v1/admin/customers/{customer_user.id}/stock-exclusion", headers=auth_header(customer_user), json={"enabled": False}
    )).status_code == 403


async def test_a_branch_order_takes_no_stock_even_at_zero_and_puts_none_back(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    await _set_stock(db_session, variant_id, 0)
    # An ordinary customer cannot buy what the site has none of…
    refused = await _checkout(client, admin_user, variant_id, 3)
    assert refused.status_code == 422, refused.text

    # …a branch can: it sells from its own shelf.
    await _branch(client, admin_user, customer_user)
    placed = await _checkout(client, customer_user, variant_id, 3)
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]
    assert await _stock(db_session, variant_id) == 0
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.exclude_from_stock_and_reports is True
    lines = (await db_session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))).all()
    assert [line.stock_warehouse for line in lines] == [None]

    h = auth_header(admin_user)
    approved = await client.post(f"/api/v1/admin/orders/{order_id}/approve", headers=h, json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["loyalty_points_awarded"] == 0  # no points for a branch sale
    assert await _stock(db_session, variant_id) == 0

    cancelled = await client.post(f"/api/v1/admin/orders/{order_id}/cancel", headers=h, json={"reason": "şube iptali"})
    assert cancelled.status_code == 200, cancelled.text
    assert await _stock(db_session, variant_id) == 0  # nothing put back


async def test_an_order_keeps_the_flag_it_was_placed_under(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    await _branch(client, admin_user, customer_user)
    branch = (await _checkout(client, customer_user, variant_id, 1)).json()
    await _branch(client, admin_user, customer_user, enabled=False)
    before = await _stock(db_session, variant_id)
    online = (await _checkout(client, customer_user, variant_id, 2)).json()
    assert await _stock(db_session, variant_id) == before - 2  # the flag off: stock taken again

    assert (await db_session.get(Order, branch["id"], populate_existing=True)).exclude_from_stock_and_reports is True
    assert (await db_session.get(Order, online["id"], populate_existing=True)).exclude_from_stock_and_reports is False
    # Cancelling the branch order now still puts nothing back.
    await client.post(f"/api/v1/admin/orders/{branch['id']}/cancel", headers=auth_header(admin_user), json={"reason": "x"})
    assert await _stock(db_session, variant_id) == before - 2


async def test_branch_sales_stay_out_of_the_dashboard_reports_and_bestsellers(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    variant_id, product_id = normal_v.id, normal_v.product_id
    h = auth_header(admin_user)

    async def figures():
        dash = (await client.get("/api/v1/admin/dashboard", headers=h)).json()
        summary = (await client.get("/api/v1/admin/reports/summary", headers=h)).json()
        best = (await client.get("/api/v1/products/bestsellers", params={"limit": 50})).json()
        return dash["daily"], summary, {p["id"] for p in best}

    day0, summary0, _ = await figures()
    await _branch(client, admin_user, customer_user)
    order = (await _checkout(client, customer_user, variant_id, 4)).json()
    assert (await client.post(f"/api/v1/admin/orders/{order['id']}/approve", headers=h, json={})).status_code == 200

    day1, summary1, best = await figures()
    assert day1["order_count"] == day0["order_count"]
    assert Decimal(day1["order_total"]) == Decimal(day0["order_total"])
    assert summary1 == summary0
    assert str(product_id) not in best


@pytest.mark.parametrize("kind", ["all", "internal", "normal"])
async def test_the_order_list_keeps_them_with_a_badge_and_a_filter(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, kind: str
):
    normal_v, _ = catalog
    await _branch(client, admin_user, customer_user)
    order = (await _checkout(client, customer_user, normal_v.id, 1)).json()
    listed = (await client.get("/api/v1/admin/orders", headers=auth_header(admin_user), params={"kind": kind, "limit": 200})).json()
    rows = {o["id"]: o for o in listed}
    if kind == "normal":
        assert order["id"] not in rows
        assert all(not o["exclude_from_stock_and_reports"] for o in listed)
    else:
        assert rows[order["id"]]["exclude_from_stock_and_reports"] is True
        if kind == "internal":
            assert all(o["exclude_from_stock_and_reports"] for o in listed)
    counts = (await client.get("/api/v1/admin/orders/status-counts", headers=auth_header(admin_user), params={"kind": kind})).json()
    assert isinstance(counts, dict)


async def test_a_branch_order_sends_no_sms(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    await _branch(client, admin_user, customer_user)
    order = (await _checkout(client, customer_user, normal_v.id, 1)).json()
    assert await _logs(db_session, order["order_number"], sms_settings.ORDER_PLACED) == []
    # An ordinary order is texted, as before.
    online = await _checkout(client, admin_user, normal_v.id, 1)
    assert online.status_code == 201, online.text
    assert await _logs(db_session, online.json()["order_number"], sms_settings.ORDER_PLACED)


async def test_a_branch_cart_is_not_short_of_online_stock(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    await _set_stock(db_session, variant_id, 0)
    body = {"items": [{"variant_id": str(variant_id), "quantity": 5}], "payment_method": "BANK_TRANSFER", "shipping_method": "YURTICI_DOOR"}
    ordinary = (await client.post("/api/v1/orders/calculate-cart", headers=auth_header(admin_user), json=body)).json()
    assert ordinary["items"][0]["stock_short"] is True
    await _branch(client, admin_user, customer_user)
    branch = (await client.post("/api/v1/orders/calculate-cart", headers=auth_header(customer_user), json=body)).json()
    assert branch["items"][0]["stock_short"] is False
