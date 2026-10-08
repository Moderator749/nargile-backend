"""B2B applications and the admin dashboard (Genel Bakış) KPIs.

An application is made from the account page, by a customer who is already
signed in — sign-up itself never asks for a price group, so there is nothing
here about registering with one.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin_dashboard import period_starts
from app.models.b2b import B2BApplication
from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import CustomerProfile, CustomerSegment, SegmentType, User
from tests.conftest import auth_header

TR = ZoneInfo("Europe/Istanbul")
APPLICATION = {"segment": "CAFE", "company_title": "Köşe Kafe Ltd.", "tax_number": "1234567890"}


def _register_body(**extra):
    return {
        "email": f"b2b-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Gizli-Parola-123",
        "full_name": "Kafe Sahibi",
        "phone": "0532 111 22 33",
        **extra,
    }


# ── B2B applications ─────────────────────────────────────────────────────────


async def test_an_application_waits_for_an_admin_and_grants_nothing_itself(
    client: AsyncClient, db_session: AsyncSession, customer_user: User
):
    resp = await client.post("/api/v1/me/b2b-application", headers=auth_header(customer_user), json=APPLICATION)
    assert resp.status_code == 201, resp.text
    retail = await db_session.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))
    assert customer_user.segment_id == retail  # never self-granted
    row = await db_session.scalar(select(B2BApplication).where(B2BApplication.user_id == customer_user.id))
    assert row.status == "PENDING" and row.requested_segment == "CAFE"


@pytest.mark.parametrize(
    "application",
    [
        {**APPLICATION, "tax_number": "12345"},
        {**APPLICATION, "segment": "RETAIL"},
        {**APPLICATION, "company_title": " "},
    ],
)
async def test_bad_applications_are_rejected(
    client: AsyncClient, customer_user: User, application: dict
):
    resp = await client.post("/api/v1/me/b2b-application", headers=auth_header(customer_user), json=application)
    assert resp.status_code == 422, resp.text


async def test_customer_applies_once_at_a_time(client: AsyncClient, customer_user: User):
    h = auth_header(customer_user)
    assert (await client.get("/api/v1/me/b2b-application", headers=h)).json() is None
    first = await client.post("/api/v1/me/b2b-application", headers=h, json=APPLICATION)
    assert first.status_code == 201 and first.json()["status"] == "PENDING"
    assert (await client.post("/api/v1/me/b2b-application", headers=h, json=APPLICATION)).status_code == 409


async def test_admin_approves_with_override_and_records_company(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession
):
    user_id = customer_user.id
    created = (await client.post("/api/v1/me/b2b-application", headers=auth_header(customer_user), json=APPLICATION)).json()
    pending = (await client.get("/api/v1/admin/b2b-applications", headers=auth_header(admin_user))).json()
    assert any(a["id"] == created["id"] for a in pending)

    url = f"/api/v1/admin/b2b-applications/{created['id']}/approve"
    resp = await client.post(url, headers=auth_header(admin_user), json={"segment": "DEALER"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"
    assert (await client.post(url, headers=auth_header(admin_user), json={})).status_code == 409

    dealer = await db_session.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.DEALER))
    segment_id = await db_session.scalar(select(User.segment_id).where(User.id == user_id))
    assert segment_id == dealer
    title = await db_session.scalar(select(CustomerProfile.company_title).where(CustomerProfile.user_id == user_id))
    assert title == "Köşe Kafe Ltd."


async def test_admin_rejects_with_reason(client: AsyncClient, admin_user: User, customer_user: User):
    created = (await client.post("/api/v1/me/b2b-application", headers=auth_header(customer_user), json=APPLICATION)).json()
    url = f"/api/v1/admin/b2b-applications/{created['id']}/reject"
    assert (await client.post(url, headers=auth_header(admin_user), json={"note": ""})).status_code == 422
    resp = await client.post(url, headers=auth_header(admin_user), json={"note": "Vergi no doğrulanamadı"})
    assert resp.json()["status"] == "REJECTED" and resp.json()["admin_note"] == "Vergi no doğrulanamadı"
    mine = (await client.get("/api/v1/me/b2b-application", headers=auth_header(customer_user))).json()
    assert mine["status"] == "REJECTED"


# ── Dashboard ────────────────────────────────────────────────────────────────


def test_period_starts_use_turkey_time():
    wednesday = datetime(2026, 10, 7, 10, 0, tzinfo=TR)
    starts = period_starts(wednesday)
    assert starts["daily"] == datetime(2026, 10, 7, tzinfo=TR)  # today 00:00
    assert starts["weekly"] == datetime(2026, 10, 5, tzinfo=TR)  # Monday
    assert starts["monthly"] == datetime(2026, 10, 1, tzinfo=TR)
    assert starts["yearly"] == datetime(2026, 1, 1, tzinfo=TR)
    # Sunday 21:30 UTC is already Monday 00:30 in Istanbul: a new week.
    late_sunday_utc = period_starts(datetime(2026, 10, 11, 21, 30, tzinfo=timezone.utc))
    assert late_sunday_utc["weekly"] == datetime(2026, 10, 12, tzinfo=TR)
    assert late_sunday_utc["daily"] == datetime(2026, 10, 12, tzinfo=TR)


async def _dashboard(client, admin):
    resp = await client.get("/api/v1/admin/dashboard", headers=auth_header(admin))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _order(user, method, status, paid_at, net: str) -> Order:
    return Order(
        order_number=f"T-{uuid.uuid4().hex[:10]}", user_id=user.id,
        total_amount=Decimal(net), shipping_cost=Decimal("0"), discount_amount=Decimal("0"),
        payment_method=method, status=status, paid_at=paid_at,
    )


async def test_dashboard_counts_only_paid_orders(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession
):
    before = await _dashboard(client, admin_user)
    now = datetime.now(timezone.utc)
    last_year = datetime(now.astimezone(TR).year - 1, 6, 1, tzinfo=TR)
    # Paid before today's midnight (Turkey time): in the weekly/monthly totals but not daily,
    # unless today is Monday / the 1st (then it falls outside those too).
    yesterday = period_starts(now)["daily"] - timedelta(hours=1)
    db_session.add_all([
        _order(customer_user, PaymentMethod.CREDIT_CARD, OrderStatus.APPROVED, now, "1000.00"),
        _order(customer_user, PaymentMethod.BANK_TRANSFER, OrderStatus.SHIPPED, now, "2500.00"),
        _order(customer_user, PaymentMethod.BANK_TRANSFER, OrderStatus.CANCELLED, None, "9999.00"),
        _order(customer_user, PaymentMethod.BANK_TRANSFER, OrderStatus.AWAITING_TRANSFER, None, "7777.00"),
        _order(customer_user, PaymentMethod.CREDIT_CARD, OrderStatus.APPROVED, last_year, "500.00"),
        _order(customer_user, PaymentMethod.CREDIT_CARD, OrderStatus.APPROVED, yesterday, "300.00"),
    ])
    await db_session.commit()
    after = await _dashboard(client, admin_user)

    starts = period_starts(now)
    for period in ("daily", "weekly", "monthly", "yearly"):
        d = lambda k: Decimal(after[period][k]) - Decimal(before[period][k])  # noqa: E731
        # Yesterday's ₺300 card order counts wherever "yesterday" is inside the period.
        extra = 1 if yesterday >= starts[period] else 0
        assert after[period]["order_count"] - before[period]["order_count"] == 2 + extra, period
        assert d("order_total") == Decimal("3500.00") + 300 * extra, period
        assert d("card_total") == Decimal("1000.00") + 300 * extra, period
        assert d("transfer_total") == Decimal("2500.00"), period
    assert after["member_count"] >= 1


async def test_dashboard_counts_pending_applications(client: AsyncClient, admin_user: User, customer_user: User):
    before = (await _dashboard(client, admin_user))["pending_applications"]
    await client.post("/api/v1/me/b2b-application", headers=auth_header(customer_user), json=APPLICATION)
    assert (await _dashboard(client, admin_user))["pending_applications"] == before + 1


async def test_admin_orders_report_total(client: AsyncClient, admin_user: User):
    resp = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user), params={"limit": 1})
    assert int(resp.headers["x-total-count"]) >= len(resp.json())


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/v1/admin/dashboard"), ("GET", "/api/v1/admin/b2b-applications"), ("POST", "/api/v1/admin/b2b-applications/1/approve")],
)
async def test_dashboard_routes_require_admin(client: AsyncClient, customer_user: User, method: str, path: str):
    assert (await client.request(method, path, headers=auth_header(customer_user), json={})).status_code == 403
