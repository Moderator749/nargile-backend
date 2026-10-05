"""Tests for checkout engine business rules.

Covers:
- Bank transfer discount tiers (5% / 7%)
- Credit card = no discount
- HEAVY_CARGO anti-fraud cargo algorithm (>50% threshold)
- ParaPuan loyalty earning calculation
- Full calculate-cart HTTP endpoint integration
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from app.models.catalog import ProductVariant
from app.models.order import PaymentMethod
from app.models.user import User
from app.services.checkout_engine import CheckoutEngine, ResolvedCartItem
from tests.conftest import auth_header


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_item(line_total: str, tags: list[str]) -> ResolvedCartItem:
    item = MagicMock(spec=ResolvedCartItem)
    item.line_total = Decimal(line_total)
    item.tags = tags
    return item


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
# ANTI-FRAUD CARGO ALGORITHM
# ═══════════════════════════════════════════════════════════════════════════════


class TestCargoAntifraud:
    def test_free_shipping_above_threshold_no_heavy(self):
        items = [_mock_item("4000", [])]
        cost = CheckoutEngine.calculate_cargo(Decimal("4000"), items)
        assert cost == Decimal("0.00")

    def test_flat_rate_below_threshold_no_heavy(self):
        items = [_mock_item("1000", [])]
        cost = CheckoutEngine.calculate_cargo(Decimal("1000"), items)
        assert cost == Decimal("49.90")

    def test_heavy_below_50_pct_above_threshold_gets_surcharge(self):
        """Heavy cargo present but <= 50% of cart → heavy surcharge, not flat."""
        items = [_mock_item("2800", []), _mock_item("500", ["HEAVY_CARGO"])]
        cost = CheckoutEngine.calculate_cargo(Decimal("3300"), items)
        assert cost == Decimal("89.90")

    def test_heavy_above_50_pct_triggers_antifraud(self):
        """HEAVY_CARGO > 50% of cart value → always surcharge, even above 3k."""
        items = [_mock_item("1000", []), _mock_item("4000", ["HEAVY_CARGO"])]
        cost = CheckoutEngine.calculate_cargo(Decimal("5000"), items)
        assert cost == Decimal("89.90")

    def test_heavy_exactly_50_pct_is_not_antifraud(self):
        """Exactly 50% should NOT trigger anti-fraud (strictly >50%)."""
        items = [_mock_item("2000", []), _mock_item("2000", ["HEAVY_CARGO"])]
        cost = CheckoutEngine.calculate_cargo(Decimal("4000"), items)
        # Not anti-fraud, but above threshold + has heavy → heavy surcharge
        assert cost == Decimal("89.90")

    def test_100_pct_heavy_always_surcharge(self):
        """Cart entirely heavy cargo → always surcharge."""
        items = [_mock_item("10000", ["HEAVY_CARGO"])]
        cost = CheckoutEngine.calculate_cargo(Decimal("10000"), items)
        assert cost == Decimal("89.90")

    def test_heavy_below_threshold_still_surcharge(self):
        """Heavy cargo below 3k → always surcharge regardless."""
        items = [_mock_item("500", ["HEAVY_CARGO"])]
        cost = CheckoutEngine.calculate_cargo(Decimal("500"), items)
        assert cost == Decimal("89.90")


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


async def test_calculate_cart_heavy_antifraud(
    client: AsyncClient,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """1 normal (5k) + 20 heavy (20k) = 25k → heavy 80% > 50% → surcharge."""
    normal_v, heavy_v = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(admin_user),
        json={
            "payment_method": "CREDIT_CARD",
            "items": [
                {"variant_id": str(normal_v.id), "quantity": 1},
                {"variant_id": str(heavy_v.id), "quantity": 20},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(data["subtotal"]) == Decimal("25000.00")
    assert Decimal(data["cargo_cost"]) == Decimal("89.90")
    assert Decimal(data["discount_amount"]) == Decimal("0.00")
    assert data["loyalty_points_to_earn"] == 2500
