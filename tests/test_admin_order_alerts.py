"""The panel's new-order chime: /admin/orders/latest lists the orders placed
since a moment — a transfer at checkout, a card order once paid."""

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.user import User
from tests.conftest import ADDRESS, CONSENTS, auth_header

URL = "/api/v1/admin/orders/latest"


async def _checkout(client: AsyncClient, user: User, variant, payment: str = "BANK_TRANSFER") -> dict:
    body = {
        **CONSENTS,
        "payment_method": payment,
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_address": ADDRESS,
        "shipping_consent": True,
    }
    resp = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _latest(client: AsyncClient, admin: User, since: str | None) -> dict:
    resp = await client.get(URL, headers=auth_header(admin), params={"since": since} if since else {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(body: dict) -> list[str]:
    return [o["id"] for o in body["orders"]]


async def test_the_first_poll_only_gives_the_clock(client: AsyncClient, admin_user: User, customer_user: User, catalog):
    await _checkout(client, customer_user, catalog[0])
    body = await _latest(client, admin_user, None)
    assert body["orders"] == [] and body["now"]


async def test_a_transfer_order_is_announced_once_placed(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    start = (await _latest(client, admin_user, None))["now"]
    order = await _checkout(client, customer_user, catalog[0])
    body = await _latest(client, admin_user, start)
    [new] = [o for o in body["orders"] if o["id"] == order["id"]]
    assert new["order_number"] == order["order_number"]
    assert new["total_amount"] == order["total_amount"]
    assert new["customer_name"]

    # Well past it (beyond the overlap), it is not listed again.
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert order["id"] not in _ids(await _latest(client, admin_user, later))


async def test_a_card_order_is_announced_only_once_paid(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession, catalog
):
    start = (await _latest(client, admin_user, None))["now"]
    order = await _checkout(client, customer_user, catalog[0], payment="CREDIT_CARD")
    assert order["id"] not in _ids(await _latest(client, admin_user, start))

    row = await db_session.get(Order, order["id"])
    row.paid_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert order["id"] in _ids(await _latest(client, admin_user, start))


async def test_only_an_admin_polls(client: AsyncClient, customer_user: User):
    assert (await client.get(URL, headers=auth_header(customer_user))).status_code == 403


async def test_a_time_without_a_zone_is_refused(client: AsyncClient, admin_user: User):
    resp = await client.get(URL, headers=auth_header(admin_user), params={"since": "2026-11-10T10:00:00"})
    assert resp.status_code == 422
