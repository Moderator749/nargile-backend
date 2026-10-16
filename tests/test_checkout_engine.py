"""Tests for checkout engine business rules.

Covers:
- Volume ("ciro") discount tiers (5% / 8%)
- Bank transfer discount tiers (5% / 7%), applied after the volume discount
- Credit card = no transfer discount
- Cargo: 3,000 TRY free shipping; 20 kg coal minimum only for coal-dominant carts (>50%)
- ParaPuan loyalty earning calculation
- Full calculate-cart HTTP endpoint integration
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.catalog import Category, Product, ProductVariant
from app.models.order import PaymentMethod, ShippingMethod
from app.models.user import User
from app.services.checkout_engine import CheckoutEngine, ResolvedCartItem
from tests.conftest import ADDRESS, CONSENTS, auth_header


# ── Helpers ──────────────────────────────────────────────────────────────────


def _item(
    line_total: str, *, coal: bool = False, kg: str | None = None, qty: int = 1, tags: list[str] | None = None
) -> ResolvedCartItem:
    """A real cart line: `line_total` spread over `qty` units of `kg` each."""
    return ResolvedCartItem(
        variant=MagicMock(),
        product=SimpleNamespace(tags=tags or []),
        unit_price=Decimal(line_total) / qty,
        quantity=qty,
        is_coal=coal,
        unit_weight_kg=Decimal(kg) if kg is not None else None,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BANK TRANSFER DISCOUNT
# ═══════════════════════════════════════════════════════════════════════════════


class TestBankTransferDiscount:
    def test_5_percent_discount_low_range(self):
        rate, amt = CheckoutEngine.calculate_discounts(
            Decimal("15000.00"), PaymentMethod.BANK_TRANSFER
        )
        assert rate == Decimal("0.05")
        assert amt == Decimal("750.00")

    def test_5_percent_at_boundary_29999(self):
        rate, amt = CheckoutEngine.calculate_discounts(
            Decimal("29999.99"), PaymentMethod.BANK_TRANSFER
        )
        assert rate == Decimal("0.05")
        assert amt == Decimal("1500.00")

    def test_7_percent_at_exactly_30000(self):
        rate, amt = CheckoutEngine.calculate_discounts(
            Decimal("30000.00"), PaymentMethod.BANK_TRANSFER
        )
        assert rate == Decimal("0.07")
        assert amt == Decimal("2100.00")

    def test_7_percent_high_value(self):
        rate, amt = CheckoutEngine.calculate_discounts(
            Decimal("100000.00"), PaymentMethod.BANK_TRANSFER
        )
        assert rate == Decimal("0.07")
        assert amt == Decimal("7000.00")

    def test_no_discount_below_1_try(self):
        rate, amt = CheckoutEngine.calculate_discounts(
            Decimal("0.50"), PaymentMethod.BANK_TRANSFER
        )
        assert rate == Decimal("0.00")
        assert amt == Decimal("0.00")

    def test_credit_card_never_gets_discount(self):
        rate, amt = CheckoutEngine.calculate_discounts(
            Decimal("50000.00"), PaymentMethod.CREDIT_CARD
        )
        assert rate == Decimal("0.00")
        assert amt == Decimal("0.00")


# ═══════════════════════════════════════════════════════════════════════════════
# VOLUME ("CIRO") DISCOUNT
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("subtotal", "rate", "amount"),
    [
        ("29999.99", "0.00", "0.00"),
        ("30000.00", "0.05", "1500.00"),
        ("49999.99", "0.05", "2500.00"),
        ("50000.00", "0.08", "4000.00"),
        ("100000.00", "0.08", "8000.00"),
    ],
)
def test_volume_discount_tiers(subtotal: str, rate: str, amount: str):
    assert CheckoutEngine.calculate_volume_discount(Decimal(subtotal)) == (Decimal(rate), Decimal(amount))


def test_transfer_discount_is_sequential_after_volume():
    """35,000 by transfer: ciro 5% = 1,750, then 7% of the remaining 33,250 = 2,327.50."""
    rate, volume = CheckoutEngine.calculate_volume_discount(Decimal("35000"))
    t_rate, transfer = CheckoutEngine.calculate_discounts(
        Decimal("35000"), PaymentMethod.BANK_TRANSFER, base=Decimal("35000") - volume
    )
    assert (rate, volume) == (Decimal("0.05"), Decimal("1750.00"))
    assert (t_rate, transfer) == (Decimal("0.07"), Decimal("2327.50"))


def test_transfer_tier_is_judged_on_the_subtotal():
    """31,000: the volume discount brings the base under 30k, but the 7% tier still applies."""
    _, volume = CheckoutEngine.calculate_volume_discount(Decimal("31000"))
    rate, amount = CheckoutEngine.calculate_discounts(
        Decimal("31000"), PaymentMethod.BANK_TRANSFER, base=Decimal("31000") - volume
    )
    assert rate == Decimal("0.07")
    assert amount == Decimal("2061.50")  # 7% of 29,450


# ═══════════════════════════════════════════════════════════════════════════════
# CARGO & COAL RULES
# ═══════════════════════════════════════════════════════════════════════════════


class TestCargo:
    def quote(self, *items: ResolvedCartItem):
        return CheckoutEngine.quote_shipping(sum((i.line_total for i in items), Decimal("0")), list(items))

    def test_free_from_threshold_without_coal(self):
        q = self.quote(_item("3000"))
        assert (q.cost, q.reason, q.free) == (Decimal("0.00"), "free", True)

    def test_below_threshold_is_never_charged_but_the_receiver_pays(self):
        q = self.quote(_item("2999.99"))
        assert (q.cost, q.reason) == (Decimal("0.00"), "below_threshold")
        assert q.receiver_pays and not q.free

    def test_heavy_cargo_below_threshold_is_not_charged_either(self):
        """Carriage is never added to the order, whatever the freight class."""
        q = self.quote(_item("500", tags=["HEAVY_CARGO"]))
        assert (q.cost, q.reason, q.receiver_pays) == (Decimal("0.00"), "below_threshold", True)

    def test_coal_below_threshold_is_the_receivers_to_pay(self):
        q = self.quote(_item("2000", coal=True, kg="1", qty=40))
        assert (q.cost, q.reason, q.receiver_pays) == (Decimal("0.00"), "below_threshold", True)

    def test_the_istanbul_run_is_free_from_its_own_limit(self):
        """The shop's own van: free from the limit the panel sets (₺20,000 by
        default), and below it not an option at all — `resolve_cart` refuses it
        there, which is what the API tests walk through."""
        q = CheckoutEngine.quote_shipping(
            Decimal("20000"), [_item("20000")], ShippingMethod.ISTANBUL_FREE, perks=False
        )
        assert (q.cost, q.reason, q.free) == (Decimal("0.00"), "free", True)

        below = CheckoutEngine.quote_shipping(
            Decimal("100"), [_item("100")], ShippingMethod.ISTANBUL_FREE, perks=False
        )
        assert (below.reason, below.free) == ("below_threshold", False)

    # Scenario A: hardware/mixed dominant (coal ≤ 50%) → the 20 kg rule doesn't apply.

    def test_steel_set_with_two_boxes_of_coal_ships_free(self):
        """The reported case: ~₺10,000 set + 2 boxes of coal (~8%) → free, no 20 kg needed."""
        q = self.quote(_item("9990"), _item("880", coal=True, kg="1", qty=2))
        assert (q.cost, q.reason) == (Decimal("0.00"), "free")

    def test_coal_exactly_half_ships_free_regardless_of_weight(self):
        q = self.quote(_item("2000"), _item("2000", coal=True, kg="1", qty=5))
        assert q.reason == "free"

    def test_coal_unknown_weight_is_irrelevant_when_not_dominant(self):
        q = self.quote(_item("3000"), _item("1000", coal=True, kg=None, qty=50))
        assert q.reason == "free"

    # Scenario B: coal dominant (> 50%) → free only from 20 kg.

    def test_coal_dominant_under_20kg_is_the_receivers_to_pay(self):
        q = self.quote(_item("1000"), _item("4000", coal=True, kg="1", qty=19))
        assert (q.cost, q.reason, q.receiver_pays) == (Decimal("0.00"), "coal_weight", True)
        assert q.coal_ratio == Decimal("0.8000")

    def test_coal_dominant_from_20kg_ships_free(self):
        q = self.quote(_item("1000"), _item("4000", coal=True, kg="1", qty=20))
        assert (q.cost, q.reason) == (Decimal("0.00"), "free")

    def test_coal_only_order_from_20kg_ships_free(self):
        q = self.quote(_item("4400", coal=True, kg="1", qty=20))
        assert q.reason == "free"

    def test_coal_dominant_unknown_weight_counts_as_zero(self):
        q = self.quote(_item("5000", coal=True, kg=None, qty=50))
        assert (q.reason, q.coal_weight_kg, q.coal_weight_unknown) == ("coal_weight", Decimal("0.000"), True)


# ═══════════════════════════════════════════════════════════════════════════════
# PARAPUAN LOYALTY EARNINGS
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoyaltyEarnings:
    def test_earns_10_pct_at_20k(self):
        assert CheckoutEngine.calculate_loyalty_earnings(Decimal("20000.00")) == 2000

    def test_earns_10_pct_at_29999(self):
        assert CheckoutEngine.calculate_loyalty_earnings(Decimal("29999.99")) == 3000

    def test_earns_10_pct_mid_range(self):
        assert CheckoutEngine.calculate_loyalty_earnings(Decimal("25000.00")) == 2500

    def test_zero_below_threshold(self):
        assert CheckoutEngine.calculate_loyalty_earnings(Decimal("19999.99")) == 0

    def test_zero_at_30k(self):
        assert CheckoutEngine.calculate_loyalty_earnings(Decimal("30000.00")) == 0

    def test_zero_at_5k(self):
        assert CheckoutEngine.calculate_loyalty_earnings(Decimal("5000.00")) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP INTEGRATION — /calculate-cart
# ═══════════════════════════════════════════════════════════════════════════════


async def test_calculate_cart_bank_transfer_5pct(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """4 x 5000 TRY = 20,000 → bank transfer 5% discount, free shipping, 2000 pts."""
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(admin_user),
        json={
            "payment_method": "BANK_TRANSFER",
            "items": [{"variant_id": str(normal_v.id), "quantity": 4}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(data["subtotal"]) == Decimal("20000.00")
    assert Decimal(data["discount_rate"]) == Decimal("0.05")
    assert Decimal(data["discount_amount"]) == Decimal("1000.00")
    assert Decimal(data["cargo_cost"]) == Decimal("0.00")
    assert data["loyalty_points_to_earn"] == 2000
    assert "image_url" in data["items"][0]  # the cart shows product photos


async def test_calculate_cart_coal_dominant_needs_20kg(
    client: AsyncClient,
    admin_user: User,
    db_session,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """1 normal (5k) + coal at 1k/kg: 19 kg (79% coal) pays, 20 kg ships free."""
    normal_v, coal_v = catalog
    coal_v.weight_kg = Decimal("1")
    await db_session.commit()

    async def quote(kg: int):
        resp = await client.post(
            "/api/v1/orders/calculate-cart",
            headers=auth_header(admin_user),
            json={
                "payment_method": "CREDIT_CARD",
                "items": [
                    {"variant_id": str(normal_v.id), "quantity": 1},
                    {"variant_id": str(coal_v.id), "quantity": kg},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    short = await quote(19)
    assert (Decimal(short["cargo_cost"]), short["shipping"]["reason"]) == (Decimal("0.00"), "coal_weight")
    assert short["shipping"]["receiver_pays"] is True
    assert Decimal(short["shipping"]["coal_ratio"]) > Decimal("0.5")
    enough = await quote(20)
    assert (Decimal(enough["cargo_cost"]), enough["shipping"]["reason"]) == (Decimal("0.00"), "free")


async def test_coal_rule_follows_the_product_switch_not_the_category(
    client: AsyncClient,
    admin_user: User,
    db_session,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """In the Kömürler category but with "Sepet Kuralı" off, coal is ordinary: free from the threshold."""
    normal_v, coal_v = catalog
    coal = await db_session.get(Product, coal_v.product_id)
    coal.category_id = await db_session.scalar(select(Category.id).where(Category.slug == "nargile-komurleri"))
    coal.is_cart_rule_applied = False
    coal_v.weight_kg = Decimal("1")
    await db_session.commit()

    async def reason():
        resp = await client.post(
            "/api/v1/orders/calculate-cart",
            headers=auth_header(admin_user),
            json={
                "payment_method": "CREDIT_CARD",
                "items": [
                    {"variant_id": str(normal_v.id), "quantity": 1},
                    {"variant_id": str(coal_v.id), "quantity": 19},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["shipping"]

    off = await reason()
    assert (off["reason"], Decimal(off["coal_total"])) == ("free", Decimal("0.00"))

    coal.is_cart_rule_applied = True
    await db_session.commit()
    on = await reason()
    assert (on["reason"], on["receiver_pays"]) == ("coal_weight", True)


async def test_calculate_cart_hardware_dominant_ignores_coal_weight(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """5 normal (25k) + 2 coal (2k, no weight recorded) → 7% coal → free shipping."""
    normal_v, coal_v = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(admin_user),
        json={
            "payment_method": "CREDIT_CARD",
            "items": [
                {"variant_id": str(normal_v.id), "quantity": 5},
                {"variant_id": str(coal_v.id), "quantity": 2},
            ],
        },
    )
    data = resp.json()
    assert (Decimal(data["cargo_cost"]), data["shipping"]["reason"]) == (Decimal("0.00"), "free")


async def test_calculate_cart_volume_and_transfer_discounts(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """7 x 5,000 = 35,000 by transfer: −1,750 ciro, −2,327.50 havale."""
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(admin_user),
        json={"payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(normal_v.id), "quantity": 7}]},
    )
    data = resp.json()
    assert Decimal(data["volume_discount_rate"]) == Decimal("0.05")
    assert Decimal(data["volume_discount_amount"]) == Decimal("1750.00")
    assert Decimal(data["discount_amount"]) == Decimal("2327.50")
    assert Decimal(data["net_total"]) == Decimal("30922.50")
    assert data["volume_next_tier"] == {"threshold": "50000.00", "rate": "0.08"}


async def test_checkout_records_all_discounts(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """The stored order's discount covers volume + transfer (55,000 → 8% then 7%)."""
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(admin_user),
        json={**CONSENTS, 
            "payment_method": "BANK_TRANSFER",
            "items": [{"variant_id": str(normal_v.id), "quantity": 11}],
            "shipping_address": ADDRESS,
        },
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()
    # 55,000: ciro 8% = 4,400; havale 7% of 50,600 = 3,542 → 7,942
    assert Decimal(order["discount_amount"]) == Decimal("7942.00")
