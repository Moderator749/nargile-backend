"""The "Parametreler" page: the checkout's commercial rules as panel settings.

Covers:
- The defaults are the rules the engine always ran on (and are seeded as rows)
- The engine prices with whatever parameters it is given
- An admin saves a new free-shipping threshold / ciro ladder and the very next
  cart follows, as does the public read the storefront uses
- Validation: rates 0..1, positive thresholds, no duplicate rungs, admin only
- A hand-edited row falls back to its default instead of breaking the cart
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import ProductVariant
from app.models.order import PaymentMethod
from app.models.settings import ShopSetting
from app.models.user import User
from app.services.checkout_engine import CheckoutEngine
from app.services.shop_settings import ISTANBUL_FREE_MIN_KEY
from app.services.store_parameters import (
    DEFAULT_LOYALTY,
    DEFAULT_PARAMETERS,
    VOLUME_TIERS_KEY,
    StoreParameters,
    Tier,
    parameters_as_rows,
    save_parameters,
)
from tests.conftest import auth_header
from tests.test_checkout_engine import _item

URL = "/api/v1/admin/settings/parameters"
PUBLIC_URL = "/api/v1/settings/parameters"

CUSTOM = StoreParameters(
    free_shipping_threshold=Decimal("10000.00"),
    coal_min_kg=Decimal("40"),
    coal_max_ratio=Decimal("0.30"),
    bank_transfer_tiers=(Tier(Decimal("1.00"), Decimal("0.03")),),
    volume_tiers=(
        Tier(Decimal("10000.00"), Decimal("0.02")),
        Tier(Decimal("20000.00"), Decimal("0.04")),
        Tier(Decimal("40000.00"), Decimal("0.10")),
    ),
    loyalty=DEFAULT_LOYALTY,
)


def _body(params: StoreParameters) -> dict:
    def ladder(tiers: tuple[Tier, ...]) -> list[dict]:
        return [{"threshold": str(t.threshold), "rate": str(t.rate)} for t in tiers]

    return {
        "free_shipping_threshold": str(params.free_shipping_threshold),
        "coal_min_kg": str(params.coal_min_kg),
        "coal_max_ratio": str(params.coal_max_ratio),
        "coal_rule_basis": params.coal_rule_basis,
        "coal_min_quantity": params.coal_min_quantity,
        "bank_transfer_tiers": ladder(params.bank_transfer_tiers),
        "volume_tiers": ladder(params.volume_tiers),
        "loyalty": {k: (v if isinstance(v, (bool, int)) else str(v)) for k, v in params.loyalty.__dict__.items()},
        "istanbul_free_min_subtotal": str(params.istanbul_free_min),
    }


@pytest.fixture(autouse=True)
async def restore_defaults(db_session: AsyncSession):
    """The test database is shared by the whole session: put the shop's rules back."""
    yield
    await db_session.rollback()
    await save_parameters(db_session, DEFAULT_PARAMETERS)
    await db_session.commit()


async def _cart(client: AsyncClient, user: User, variant: ProductVariant, qty: int, method: str = "CREDIT_CARD"):
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(user),
        json={"payment_method": method, "items": [{"variant_id": str(variant.id), "quantity": qty}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Engine ───────────────────────────────────────────────────────────────────


def test_the_defaults_are_the_shops_rules():
    d = DEFAULT_PARAMETERS
    assert (d.free_shipping_threshold, d.coal_min_kg, d.coal_max_ratio) == (
        Decimal("3000.00"), Decimal("20"), Decimal("0.50")
    )
    assert [(t.threshold, t.rate) for t in d.volume_tiers] == [
        (Decimal("30000.00"), Decimal("0.05")), (Decimal("50000.00"), Decimal("0.08"))
    ]
    assert [(t.threshold, t.rate) for t in d.bank_transfer_tiers] == [
        (Decimal("1.00"), Decimal("0.05")), (Decimal("30000.00"), Decimal("0.07"))
    ]


@pytest.mark.parametrize(
    ("subtotal", "rate"),
    [("9999.99", "0.00"), ("10000", "0.02"), ("39999.99", "0.04"), ("40000", "0.10")],
)
def test_the_volume_discount_follows_the_ladder_it_is_given(subtotal: str, rate: str):
    got, amount = CheckoutEngine.calculate_volume_discount(Decimal(subtotal), CUSTOM)
    assert got == Decimal(rate)
    assert amount == (Decimal(subtotal) * Decimal(rate)).quantize(Decimal("0.01"))


def test_the_transfer_discount_follows_the_ladder_it_is_given():
    assert CheckoutEngine.calculate_discounts(
        Decimal("50000"), PaymentMethod.BANK_TRANSFER, params=CUSTOM
    ) == (Decimal("0.03"), Decimal("1500.00"))


def test_an_empty_ladder_switches_the_discount_off():
    off = StoreParameters(**{**DEFAULT_PARAMETERS.__dict__, "volume_tiers": (), "bank_transfer_tiers": ()})
    assert CheckoutEngine.calculate_volume_discount(Decimal("100000"), off) == (Decimal("0.00"), Decimal("0.00"))
    assert CheckoutEngine.calculate_discounts(
        Decimal("100000"), PaymentMethod.BANK_TRANSFER, params=off
    ) == (Decimal("0.00"), Decimal("0.00"))


def test_shipping_follows_the_threshold_and_coal_rule_it_is_given():
    def quote(*items):
        return CheckoutEngine.quote_shipping(
            sum((i.line_total for i in items), Decimal("0")), list(items), params=CUSTOM
        )

    assert quote(_item("9999.99")).reason == "below_threshold"
    assert quote(_item("10000")).reason == "free"
    # 35% coal is dominant under a 30% ratio, and 39 kg is short of 40.
    assert quote(_item("6500"), _item("3500", coal=True, kg="1", qty=39)).reason == "coal_weight"
    assert quote(_item("6500"), _item("3500", coal=True, kg="1", qty=40)).reason == "free"


def test_the_fifty_percent_rule_counts_units_when_set_to_quantity():
    """Adet: the units decide and the weight is ignored — the two minimums never both apply."""
    by_unit = StoreParameters(**{**CUSTOM.__dict__, "coal_rule_basis": "quantity", "coal_min_quantity": 12})

    def quote(*items):
        return CheckoutEngine.quote_shipping(
            sum((i.line_total for i in items), Decimal("0")), list(items), params=by_unit
        )

    # 1,000 kg of coal over 11 units is still short of 12 units.
    short = quote(_item("6500"), _item("3500", coal=True, kg="100", qty=11))
    assert (short.reason, short.coal_quantity) == ("coal_weight", 11)
    # 12 units with no recorded weight at all: enough.
    assert quote(_item("6500"), _item("3500", coal=True, kg=None, qty=12)).reason == "free"
    # Below the ratio the minimum does not matter.
    assert quote(_item("8000"), _item("2000", coal=True, qty=1)).reason == "free"


# ── API ──────────────────────────────────────────────────────────────────────


async def test_the_parameters_are_seeded_and_published_without_a_login(
    client: AsyncClient, db_session: AsyncSession
):
    for key, value in parameters_as_rows(DEFAULT_PARAMETERS).items():
        row = await db_session.get(ShopSetting, key)
        assert row is not None and row.value == value

    resp = await client.get(PUBLIC_URL)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert Decimal(data["free_shipping_threshold"]) == Decimal("3000")
    assert [Decimal(t["rate"]) for t in data["volume_tiers"]] == [Decimal("0.05"), Decimal("0.08")]


async def test_an_admin_changes_the_rules_and_the_next_cart_follows(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Two items at ₺5,000: free shipping and no ciro discount by default; with
    a ₺12,000 threshold and a 4% rung at ₺10,000, the same cart flips both."""
    normal, _ = catalog
    before = await _cart(client, admin_user, normal, 2)
    assert before["shipping"]["reason"] == "free"
    assert Decimal(before["volume_discount_rate"]) == 0

    params = StoreParameters(
        **{
            **DEFAULT_PARAMETERS.__dict__,
            "free_shipping_threshold": Decimal("12000.00"),
            "volume_tiers": (Tier(Decimal("10000.00"), Decimal("0.04")), Tier(Decimal("60000.00"), Decimal("0.09"))),
        }
    )
    saved = await client.put(URL, headers=auth_header(admin_user), json=_body(params))
    assert saved.status_code == 200, saved.text

    after = await _cart(client, admin_user, normal, 2)
    assert after["shipping"]["reason"] == "below_threshold"
    assert Decimal(after["shipping"]["free_threshold"]) == Decimal("12000.00")
    assert Decimal(after["volume_discount_rate"]) == Decimal("0.04")
    assert Decimal(after["volume_discount_amount"]) == Decimal("400.00")
    assert Decimal(after["volume_next_tier"]["threshold"]) == Decimal("60000.00")

    public = (await client.get(PUBLIC_URL)).json()
    assert Decimal(public["free_shipping_threshold"]) == Decimal("12000.00")


async def test_the_fifty_percent_rule_can_be_switched_to_units(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """1 normal (₺5,000) + 19 coal (₺19,000, no weight): short by kilograms;
    by units the minimum decides — 19 is enough for 19, not for 20."""
    normal, coal = catalog

    async def reason() -> dict:
        resp = await client.post(
            "/api/v1/orders/calculate-cart",
            headers=auth_header(admin_user),
            json={
                "payment_method": "CREDIT_CARD",
                "items": [
                    {"variant_id": str(normal.id), "quantity": 1},
                    {"variant_id": str(coal.id), "quantity": 19},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["shipping"]

    assert (await reason())["reason"] == "coal_weight"  # 0 kg of 20

    async def by_units(minimum: int) -> dict:
        body = {**_body(DEFAULT_PARAMETERS), "coal_rule_basis": "quantity", "coal_min_quantity": minimum}
        saved = await client.put(URL, headers=auth_header(admin_user), json=body)
        assert saved.status_code == 200, saved.text
        assert (saved.json()["coal_rule_basis"], saved.json()["coal_min_quantity"]) == ("quantity", minimum)
        return await reason()

    enough = await by_units(19)
    assert (enough["reason"], enough["coal_rule_basis"], enough["coal_quantity"]) == ("free", "quantity", 19)
    short = await by_units(20)
    assert (short["reason"], short["coal_min_quantity"]) == ("coal_weight", 20)
    public = (await client.get(PUBLIC_URL)).json()
    assert (public["coal_rule_basis"], public["coal_min_quantity"]) == ("quantity", 20)


async def test_tiers_are_saved_in_order(client: AsyncClient, admin_user: User):
    body = _body(DEFAULT_PARAMETERS)
    body["volume_tiers"] = list(reversed(body["volume_tiers"]))
    resp = await client.put(URL, headers=auth_header(admin_user), json=body)
    assert resp.status_code == 200, resp.text
    assert [Decimal(t["threshold"]) for t in resp.json()["volume_tiers"]] == [Decimal("30000"), Decimal("50000")]


@pytest.mark.parametrize(
    "change",
    [
        {"free_shipping_threshold": "-1"},
        {"coal_max_ratio": "1.5"},
        {"coal_min_kg": "-5"},
        {"coal_rule_basis": "litre"},
        {"coal_min_quantity": -1},
        {"volume_tiers": [{"threshold": "30000", "rate": "1.2"}]},
        {"volume_tiers": [{"threshold": "0", "rate": "0.05"}]},
        {"volume_tiers": [{"threshold": "30000", "rate": "0.05"}, {"threshold": "30000", "rate": "0.08"}]},
        {"bank_transfer_tiers": [{"threshold": "1", "rate": "-0.01"}]},
        {"volume_tiers": [{"threshold": str(1000 * (i + 1)), "rate": "0.01"} for i in range(11)]},
    ],
)
async def test_impossible_parameters_are_refused(client: AsyncClient, admin_user: User, change: dict):
    resp = await client.put(URL, headers=auth_header(admin_user), json={**_body(DEFAULT_PARAMETERS), **change})
    assert resp.status_code == 422, resp.text


async def test_a_customer_cannot_read_or_change_the_parameters(client: AsyncClient, customer_user: User):
    headers = auth_header(customer_user)
    assert (await client.put(URL, headers=headers, json=_body(CUSTOM))).status_code == 403
    assert (await client.get(URL, headers=headers)).status_code == 403


async def test_an_unreadable_stored_ladder_falls_back_to_the_default(
    client: AsyncClient, db_session: AsyncSession
):
    row = await db_session.get(ShopSetting, VOLUME_TIERS_KEY)
    row.value = '[{"threshold": "otuz bin", "rate": "0.05"}]'
    await db_session.commit()

    data = (await client.get(PUBLIC_URL)).json()
    assert [Decimal(t["threshold"]) for t in data["volume_tiers"]] == [Decimal("30000"), Decimal("50000")]


async def test_the_istanbul_limit_is_saved_with_the_parameters(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    """"Teslimat Ayarları" moved onto this page: the same row the cart reads."""
    body = {**_body(DEFAULT_PARAMETERS), "istanbul_free_min_subtotal": "15000"}
    resp = await client.put(URL, headers=auth_header(admin_user), json=body)
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["istanbul_free_min_subtotal"]) == Decimal("15000.00")
    row = await db_session.get(ShopSetting, ISTANBUL_FREE_MIN_KEY)
    await db_session.refresh(row)
    assert row.value == "15000.00"
    public = await client.get("/api/v1/settings/shipping")
    assert Decimal(public.json()["istanbul_free_min_subtotal"]) == Decimal("15000.00")
    bad = await client.put(URL, headers=auth_header(admin_user), json={**body, "istanbul_free_min_subtotal": "-1"})
    assert bad.status_code == 422
