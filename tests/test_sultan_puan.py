"""Sultan Puan: the loyalty rules as store parameters.

Covers:
- The standard award follows the band and rate it is given (defaults = the old ParaPuan)
- Redemption at a conversion rate, capped at what the cart can absorb
- The program switch: nothing earned, nothing spent while it is off
- The annual bonus on a rolling window: under / exactly at the target, repeat
  orders after a bonus, orders older than the window, the order ceiling
- Loyalty parameters saved from the panel land in shop_settings and are validated
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order
from app.models.settings import ShopSetting
from app.models.user import User
from app.services.checkout_engine import CheckoutEngine
from app.services.store_parameters import (
    DEFAULT_LOYALTY,
    DEFAULT_PARAMETERS,
    LOYALTY_CONVERSION_KEY,
    LOYALTY_ENABLED_KEY,
    LoyaltyParameters,
    save_parameters,
)
from tests.conftest import auth_header
from tests.test_operations import _approve, _checkout

URL = "/api/v1/admin/settings/parameters"


@pytest.fixture(autouse=True)
async def restore_defaults(db_session: AsyncSession):
    """The test database is shared by the whole session: put the shop's rules back."""
    yield
    await db_session.rollback()
    await save_parameters(db_session, DEFAULT_PARAMETERS)
    await db_session.commit()


async def _set_loyalty(client: AsyncClient, admin: User, **changes) -> dict:
    """Save the loyalty card from the panel, the way the admin page does."""
    params = (await client.get(URL, headers=auth_header(admin))).json()
    params["loyalty"].update({k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items()})
    resp = await client.put(URL, headers=auth_header(admin), json=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["loyalty"]


async def _wallet(client: AsyncClient, user: User) -> dict:
    return (await client.get("/api/v1/me/wallet", headers=auth_header(user))).json()


async def _paid_order(client, admin, customer, variant, qty=1) -> int:
    """Check out and approve; the points the approval granted."""
    order = (await _checkout(client, customer, variant, qty=qty)).json()
    return (await _approve(client, admin, order["id"])).json()["loyalty_points_awarded"]


# ── Standard award ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("subtotal", "points"),
    [("19999.99", 0), ("20000.00", 2000), ("25000.00", 2500), ("29999.99", 3000), ("30000.00", 0)],
)
def test_the_default_band_is_the_old_parapuan(subtotal: str, points: int):
    assert CheckoutEngine.calculate_loyalty_earnings(Decimal(subtotal)) == points


def test_the_award_follows_the_band_and_rate_it_is_given():
    flat = replace(DEFAULT_LOYALTY, base_rate=Decimal("0.02"), band_min=Decimal("0"), band_max=Decimal("10000000"))
    assert CheckoutEngine.calculate_loyalty_earnings(Decimal("1500"), flat) == 30
    assert CheckoutEngine.calculate_loyalty_earnings(Decimal("40000"), flat) == 800
    off = replace(DEFAULT_LOYALTY, enabled=False)
    assert CheckoutEngine.calculate_loyalty_earnings(Decimal("25000"), off) == 0


# ── Redemption ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("points", "cap", "rate", "discount", "used"),
    [
        (1000, "5000", "1.00", "1000.00", 1000),
        (1000, "5000", "0.50", "500.00", 1000),  # half a lira a point
        (1000, "300", "0.50", "300.00", 600),  # the cart absorbs 600 of them
        (2000, "1234.56", "1.00", "1234.00", 1234),  # a point is never spent for less than its worth
        (100, "5000", "2.50", "250.00", 100),
    ],
)
def test_redemption_uses_the_conversion_rate(points, cap, rate, discount, used):
    loyalty = replace(DEFAULT_LOYALTY, conversion_rate=Decimal(rate))
    assert CheckoutEngine.redeem_points(points, Decimal(cap), loyalty) == (Decimal(discount), used)


async def test_a_cart_spends_points_at_the_saved_conversion_rate(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog  # ₺5,000
    db_session.add(LoyaltyLedger(
        user_id=customer_user.id, amount=1000, transaction_type=TransactionType.ADJUSTMENT,
        source=PointSource.ADMIN, expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    await db_session.commit()
    await _set_loyalty(client, admin_user, conversion_rate=Decimal("0.50"))

    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={"payment_method": "CREDIT_CARD", "points_to_redeem": 1000,
              "items": [{"variant_id": str(normal_v.id), "quantity": 1}]},
    )
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["points_discount"]) == Decimal("500.00")
    assert Decimal(resp.json()["net_total"]) == Decimal("4500.00")


# ── The switch ───────────────────────────────────────────────────────────────


async def test_switched_off_nothing_is_earned_or_spent(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    db_session.add(LoyaltyLedger(
        user_id=customer_user.id, amount=500, transaction_type=TransactionType.ADJUSTMENT,
        source=PointSource.ADMIN, expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    await db_session.commit()
    await _set_loyalty(client, admin_user, enabled=False)

    body = {"payment_method": "CREDIT_CARD", "items": [{"variant_id": str(normal_v.id), "quantity": 5}]}
    preview = await client.post("/api/v1/orders/calculate-cart", headers=auth_header(customer_user), json=body)
    assert preview.json()["loyalty_points_to_earn"] == 0  # ₺25,000: inside the band, but the program is off
    refused = await client.post(
        "/api/v1/orders/calculate-cart", headers=auth_header(customer_user), json={**body, "points_to_redeem": 100}
    )
    assert refused.status_code == 422 and "kapalı" in refused.json()["detail"]

    assert await _paid_order(client, admin_user, customer_user, normal_v, qty=5) == 0
    wallet = await _wallet(client, customer_user)
    assert wallet["enabled"] is False
    assert wallet["balance"] == 500  # the balance is kept, not lost


# ── Annual bonus ─────────────────────────────────────────────────────────────


async def test_the_annual_bonus_under_at_and_after_the_target(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    """₺5,000 orders against the default ₺20,000 target: nothing at ₺15,000, the
    bonus exactly at ₺20,000, and the next order starts counting again."""
    normal_v, _ = catalog
    awards = [await _paid_order(client, admin_user, customer_user, normal_v) for _ in range(3)]
    assert awards == [0, 0, 0]
    m = (await _wallet(client, customer_user))["milestone"]
    assert (Decimal(m["spent"]), Decimal(m["remaining"]), m["restarted"]) == (Decimal("15000.00"), Decimal("5000.00"), False)

    assert await _paid_order(client, admin_user, customer_user, normal_v) == 1000
    wallet = await _wallet(client, customer_user)
    assert wallet["history"][0]["source"] == "KUMBARA"
    assert "Yıllık Sultan Puan bonusu" in wallet["history"][0]["note"]
    assert (Decimal(wallet["milestone"]["spent"]), wallet["milestone"]["restarted"]) == (Decimal("0.00"), True)

    # Repeat orders after the bonus count toward the next one, not this one.
    assert await _paid_order(client, admin_user, customer_user, normal_v) == 0
    assert Decimal((await _wallet(client, customer_user))["milestone"]["spent"]) == Decimal("5000.00")


async def test_the_bonus_follows_the_saved_target_and_amount(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    await _set_loyalty(client, admin_user, annual_spend_threshold=Decimal("10000"), annual_bonus_points=250)
    assert [await _paid_order(client, admin_user, customer_user, normal_v) for _ in range(2)] == [0, 250]


async def test_spending_older_than_the_window_drops_out(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    for _ in range(3):
        await _paid_order(client, admin_user, customer_user, normal_v)
    # Two of the three were paid more than a year ago.
    old = (await db_session.execute(
        Order.__table__.select().where(Order.user_id == customer_user.id).order_by(Order.paid_at).limit(2)
    )).all()
    await db_session.execute(
        update(Order)
        .where(Order.id.in_([o.id for o in old]))
        .values(paid_at=datetime.now(timezone.utc) - timedelta(days=400))
    )
    await db_session.commit()

    assert Decimal((await _wallet(client, customer_user))["milestone"]["spent"]) == Decimal("5000.00")
    # ₺10,000 within the window: still short of ₺20,000, so no bonus.
    assert await _paid_order(client, admin_user, customer_user, normal_v) == 0
    # A shorter window can be saved too: 1 day still holds the two recent orders.
    await _set_loyalty(client, admin_user, annual_period_days=1)
    assert Decimal((await _wallet(client, customer_user))["milestone"]["spent"]) == Decimal("10000.00")


async def test_large_orders_count_only_without_a_ceiling(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    """By default a ₺25,000 order earns its band award and nothing toward the
    annual target, as Kumbarapuan did; with the ceiling at 0 it counts too."""
    normal_v, _ = catalog
    assert await _paid_order(client, admin_user, customer_user, normal_v, qty=5) == 2500
    assert Decimal((await _wallet(client, customer_user))["milestone"]["spent"]) == 0

    await _set_loyalty(client, admin_user, annual_order_ceiling=Decimal("0"))
    assert await _paid_order(client, admin_user, customer_user, normal_v, qty=5) == 2500 + 1000


# ── The panel ────────────────────────────────────────────────────────────────


async def test_loyalty_settings_persist_to_shop_settings(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    saved = await _set_loyalty(client, admin_user, enabled=False, conversion_rate=Decimal("0.25"))
    assert saved["enabled"] is False and Decimal(saved["conversion_rate"]) == Decimal("0.25")
    for key, value in [(LOYALTY_ENABLED_KEY, "false"), (LOYALTY_CONVERSION_KEY, "0.25")]:
        row = await db_session.get(ShopSetting, key)
        await db_session.refresh(row)
        assert (row.value, row.updated_by) == (value, admin_user.id)
    public = (await client.get("/api/v1/settings/parameters")).json()["loyalty"]
    assert public["enabled"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"base_rate": "1.5"},
        {"base_rate": "-0.1"},
        {"band_min": "30000", "band_max": "20000"},
        {"conversion_rate": "0"},
        {"conversion_rate": "500"},
        {"annual_spend_threshold": "0"},
        {"annual_bonus_points": -1},
        {"annual_period_days": 0},
        {"annual_order_ceiling": "-1"},
    ],
)
async def test_impossible_loyalty_settings_are_refused(client: AsyncClient, admin_user: User, change: dict):
    params = (await client.get(URL, headers=auth_header(admin_user))).json()
    params["loyalty"].update(change)
    assert (await client.put(URL, headers=auth_header(admin_user), json=params)).status_code == 422


async def test_unreadable_loyalty_rows_fall_back_to_the_defaults(client: AsyncClient, db_session: AsyncSession):
    for key, value in [(LOYALTY_ENABLED_KEY, "evet"), (LOYALTY_CONVERSION_KEY, "bir lira")]:
        row = await db_session.get(ShopSetting, key)
        row.value = value
    await db_session.commit()
    loyalty = (await client.get("/api/v1/settings/parameters")).json()["loyalty"]
    assert loyalty["enabled"] is True and Decimal(loyalty["conversion_rate"]) == Decimal("1.00")


def test_the_defaults_are_the_shops_rules():
    assert DEFAULT_LOYALTY == LoyaltyParameters(
        enabled=True,
        base_rate=Decimal("0.10"),
        band_min=Decimal("20000.00"),
        band_max=Decimal("29999.99"),
        conversion_rate=Decimal("1.00"),
        annual_spend_threshold=Decimal("20000.00"),
        annual_bonus_points=1000,
        annual_period_days=365,
        annual_order_ceiling=Decimal("20000.00"),
    )
