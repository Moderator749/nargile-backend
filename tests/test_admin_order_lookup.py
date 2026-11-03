"""Finding an order by its number, as Ekip Kanalı links it.

Covers:
- A whole number, in any case, finds that order alone
- Its last six characters, with or without "#", find it too
- Another exact number (an older format) still matches exactly
- No match is an empty list, counted as 0; staff and customers are refused
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import User, UserRole
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


async def _order(db: AsyncSession, user: User, number: str) -> Order:
    order = Order(
        order_number=number, user_id=user.id, total_amount=Decimal("100.00"), payment_method=PaymentMethod.CREDIT_CARD,
        status=OrderStatus.APPROVED, created_at=datetime.now(timezone.utc),
    )
    db.add(order)
    await db.commit()
    return order


async def _numbers(client: AsyncClient, admin: User, ref: str) -> tuple[list[str], str]:
    resp = await client.get("/api/v1/admin/orders", headers=auth_header(admin), params={"number": ref})
    assert resp.status_code == 200, resp.text
    return [o["order_number"] for o in resp.json()], resp.headers["x-total-count"]


async def test_an_order_is_found_by_its_number_or_its_last_six(client, db_session, admin_user, customer_user):
    suffix = uuid.uuid4().hex[:6].upper()
    number = f"ORD-20261103084112-{suffix}"
    await _order(db_session, customer_user, number)
    await _order(db_session, customer_user, f"ORD-20261103084112-{uuid.uuid4().hex[:6].upper()}")  # a neighbour

    for ref in (number, number.lower(), suffix, f"#{suffix}", f"#{suffix.lower()}", f"  {suffix} "):
        assert await _numbers(client, admin_user, ref) == ([number], "1"), ref


async def test_another_format_matches_exactly(client, db_session, admin_user, customer_user):
    legacy = f"PRV{uuid.uuid4().hex[:5].upper()}"
    await _order(db_session, customer_user, legacy)
    assert await _numbers(client, admin_user, legacy.lower()) == ([legacy], "1")
    # Six characters that are not hex are not a suffix: no partial match.
    assert await _numbers(client, admin_user, legacy[-6:].replace("V", "Z")) == ([], "0")


async def test_no_match_is_an_empty_page(client, admin_user):
    assert await _numbers(client, admin_user, "ORD-19990101000000-000000") == ([], "0")


async def test_staff_and_customers_cannot_look_orders_up(client, db_session, customer_user):
    staff = User(email=f"depo-{uuid.uuid4().hex[:6]}@test.com", hashed_password="x", role=UserRole.STAFF, is_active=True)
    db_session.add(staff)
    await db_session.commit()
    for user in (staff, customer_user):
        resp = await client.get("/api/v1/admin/orders", headers=auth_header(user), params={"number": "A1B2C3"})
        assert resp.status_code == 403
