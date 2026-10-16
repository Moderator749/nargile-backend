"""The shop's commercial parameters ("Parametreler"), edited from the panel.

These are the numbers the checkout engine used to carry as constants: the free
shipping threshold, the coal rule, the two discount ladders — havale/EFT and
"ciro" (volume) — and the Sultan Puan loyalty rules (``LoyaltyParameters``).
They live in ``shop_settings``, one row per key, and are read once per cart
calculation or order approval, so a change in the panel applies to the very
next one.

As with every setting, a row that is missing, blank or hand-edited into
nonsense falls back to its default below rather than failing the cart. The
defaults are the rules the shop has always run on, and the migration that
introduced this module seeds them as rows.
"""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import ShopSetting
from app.services.shop_settings import (
    ISTANBUL_FREE_MIN_DEFAULT,
    ISTANBUL_FREE_MIN_KEY,
    ISTANBUL_FREE_MIN_MAX,
    save_setting,
)

FREE_SHIPPING_THRESHOLD_KEY = "free_shipping_threshold"
COAL_MIN_KG_KEY = "coal_min_kg"
COAL_MAX_RATIO_KEY = "coal_max_ratio"
COAL_RULE_BASIS_KEY = "coal_rule_basis"
COAL_MIN_QUANTITY_KEY = "coal_min_quantity"
BANK_TRANSFER_TIERS_KEY = "bank_transfer_tiers"
VOLUME_TIERS_KEY = "volume_tiers"

LOYALTY_ENABLED_KEY = "sultan_puan_enabled"
LOYALTY_BASE_RATE_KEY = "sultan_puan_base_rate"
LOYALTY_BAND_MIN_KEY = "sultan_puan_band_min"
LOYALTY_BAND_MAX_KEY = "sultan_puan_band_max"
LOYALTY_CONVERSION_KEY = "sultan_puan_conversion_rate"
LOYALTY_ANNUAL_THRESHOLD_KEY = "sultan_puan_annual_spend_threshold"
LOYALTY_ANNUAL_BONUS_KEY = "sultan_puan_annual_bonus_points"
LOYALTY_ANNUAL_PERIOD_KEY = "sultan_puan_annual_period_days"
LOYALTY_ANNUAL_CEILING_KEY = "sultan_puan_annual_order_ceiling"

#: Upper bounds the panel enforces, so a slipped keypress cannot switch a rule off for good.
AMOUNT_MAX = Decimal("10000000.00")
KG_MAX = Decimal("10000")
QUANTITY_MAX = 100_000
MAX_TIERS = 10
#: 1 Sultan Puan is worth between ₺0.01 and ₺100.
CONVERSION_MIN = Decimal("0.01")
CONVERSION_MAX = Decimal("100")
BONUS_POINTS_MAX = 1_000_000
PERIOD_DAYS_MAX = 3650


#: What the "%50 Kuralı" minimum counts: the rule products' kilograms or their units (adet).
CoalRuleBasis = Literal["kg", "quantity"]


@dataclass(frozen=True)
class Tier:
    """From `threshold` TRY of merchandise subtotal upwards, `rate` (0..1) off."""

    threshold: Decimal
    rate: Decimal


def rate_for(tiers: tuple[Tier, ...], subtotal: Decimal) -> Decimal:
    """The rate of the highest tier the subtotal has reached; 0 below the first."""
    reached = [t for t in tiers if subtotal >= t.threshold]
    return max(reached, key=lambda t: t.threshold).rate if reached else Decimal("0.00")


@dataclass(frozen=True)
class LoyaltyParameters:
    """Sultan Puan: how points are earned, what they are worth, and the annual bonus."""

    # Off: nothing is earned, nothing can be spent; balances are kept.
    enabled: bool
    # Standard earning: `base_rate` (0..1) of a single paid order's subtotal,
    # for orders from `band_min` to `band_max` TRY inclusive.
    base_rate: Decimal
    band_min: Decimal
    band_max: Decimal
    # 1 Sultan Puan = this many TRY off a cart.
    conversion_rate: Decimal
    # Annual milestone: paid orders within a rolling `annual_period_days` that
    # add up to `annual_spend_threshold` earn `annual_bonus_points`, once —
    # the next bonus counts only spending after it.
    annual_spend_threshold: Decimal
    annual_bonus_points: int
    annual_period_days: int
    # Orders of this subtotal or more do not count toward the annual target
    # (they have the band, or the ciro discount); 0 counts every order.
    annual_order_ceiling: Decimal

    def counts_toward_milestone(self, subtotal: Decimal) -> bool:
        return not self.annual_order_ceiling or subtotal < self.annual_order_ceiling

    def points_for(self, subtotal: Decimal) -> int:
        """Standard Sultan Puan for one order of this subtotal."""
        if not self.enabled or not self.band_min <= subtotal <= self.band_max:
            return 0
        return int((subtotal * self.base_rate).quantize(Decimal("1")))


@dataclass(frozen=True)
class StoreParameters:
    # Retail carts from this subtotal ship on the shop.
    free_shipping_threshold: Decimal
    # "%50 Kuralı": "coal" is every product with its "Sepet Kuralı" switch on.
    # A coal-dominant cart ships on the shop only from this much coal, when the
    # rule counts kilograms (`coal_rule_basis` "kg").
    coal_min_kg: Decimal
    # Coal worth more than this share of the subtotal (0..1) makes a cart coal-dominant.
    coal_max_ratio: Decimal
    # Havale/EFT: judged on the subtotal, applied after the volume discount.
    bank_transfer_tiers: tuple[Tier, ...]
    # "Ciro indirimi", on the subtotal.
    volume_tiers: tuple[Tier, ...]
    loyalty: LoyaltyParameters
    # "İstanbul içi B2B ücretsiz teslimat": the subtotal from which the shop's
    # own van carries a firm's order for nothing (see ``shop_settings``).
    istanbul_free_min: Decimal = ISTANBUL_FREE_MIN_DEFAULT
    # Whether the %50 rule's minimum is `coal_min_kg` or `coal_min_quantity`;
    # only that one applies.
    coal_rule_basis: CoalRuleBasis = "kg"
    # The minimum in units (adet) of coal, when the rule counts units.
    coal_min_quantity: int = 20

    def volume_rate(self, subtotal: Decimal) -> Decimal:
        return rate_for(self.volume_tiers, subtotal)

    def bank_transfer_rate(self, subtotal: Decimal) -> Decimal:
        return rate_for(self.bank_transfer_tiers, subtotal)

    def next_volume_tier(self, subtotal: Decimal) -> Tier | None:
        """The lowest volume tier the cart hasn't reached ("₺X more for 8%")."""
        return next((t for t in self.volume_tiers if subtotal < t.threshold), None)


DEFAULT_PARAMETERS = StoreParameters(
    free_shipping_threshold=Decimal("3000.00"),
    coal_min_kg=Decimal("20"),
    coal_max_ratio=Decimal("0.50"),
    # Nothing below ₺1 (an empty cart), 5% up to ₺29,999.99, 7% from ₺30,000.
    bank_transfer_tiers=(
        Tier(Decimal("1.00"), Decimal("0.05")),
        Tier(Decimal("30000.00"), Decimal("0.07")),
    ),
    volume_tiers=(
        Tier(Decimal("30000.00"), Decimal("0.05")),
        Tier(Decimal("50000.00"), Decimal("0.08")),
    ),
    # The rules the shop ran as "ParaPuan": 10% on a single order of
    # ₺20,000–29,999.99, 1 point = ₺1. The annual bonus took over from
    # "Kumbarapuan" (5% once small orders reached ₺20,000, i.e. ₺1,000).
    loyalty=LoyaltyParameters(
        enabled=True,
        base_rate=Decimal("0.10"),
        band_min=Decimal("20000.00"),
        band_max=Decimal("29999.99"),
        conversion_rate=Decimal("1.00"),
        annual_spend_threshold=Decimal("20000.00"),
        annual_bonus_points=1000,
        annual_period_days=365,
        # As Kumbarapuan did: only orders under ₺20,000 add up toward it.
        annual_order_ceiling=Decimal("20000.00"),
    ),
)
DEFAULT_LOYALTY = DEFAULT_PARAMETERS.loyalty


# ── Text <-> values ──────────────────────────────────────────────────────────


def _decimal(value: str | None, default: Decimal, ceiling: Decimal, floor: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        found = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return default
    return found if found.is_finite() and floor <= found <= ceiling else default


def _int(value: str | None, default: int, ceiling: int, floor: int = 0) -> int:
    try:
        found = int((value or "").strip())
    except ValueError:
        return default
    return found if floor <= found <= ceiling else default


def _bool(value: str | None, default: bool) -> bool:
    return {"true": True, "false": False}.get((value or "").strip().lower(), default)


def _basis(value: str | None, default: CoalRuleBasis) -> CoalRuleBasis:
    found = (value or "").strip()
    return found if found in get_args(CoalRuleBasis) else default  # type: ignore[return-value]


def _tiers(value: str | None, default: tuple[Tier, ...]) -> tuple[Tier, ...]:
    """A stored ladder, sorted; the default if any rung of it is unusable."""
    if value is None:
        return default
    try:
        tiers = [Tier(Decimal(str(t["threshold"])), Decimal(str(t["rate"]))) for t in json.loads(value)]
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return default
    usable = (
        len(tiers) <= MAX_TIERS
        and len({t.threshold for t in tiers}) == len(tiers)
        and all(
            t.threshold.is_finite() and 0 < t.threshold <= AMOUNT_MAX and t.rate.is_finite() and 0 <= t.rate <= 1
            for t in tiers
        )
    )
    return tuple(sorted(tiers, key=lambda t: t.threshold)) if usable else default


def tiers_text(tiers: tuple[Tier, ...]) -> str:
    return json.dumps([{"threshold": f"{t.threshold}", "rate": f"{t.rate}"} for t in tiers])


def parameters_as_rows(params: StoreParameters) -> dict[str, str]:
    """The `shop_settings` rows that hold the commercial parameters (loyalty
    has its own: ``loyalty_as_rows``; the İstanbul limit predates this module
    and is saved by ``save_parameters`` under its own key)."""
    return {
        FREE_SHIPPING_THRESHOLD_KEY: f"{params.free_shipping_threshold}",
        COAL_MIN_KG_KEY: f"{params.coal_min_kg}",
        COAL_MAX_RATIO_KEY: f"{params.coal_max_ratio}",
        COAL_RULE_BASIS_KEY: params.coal_rule_basis,
        COAL_MIN_QUANTITY_KEY: f"{params.coal_min_quantity}",
        BANK_TRANSFER_TIERS_KEY: tiers_text(params.bank_transfer_tiers),
        VOLUME_TIERS_KEY: tiers_text(params.volume_tiers),
    }


def loyalty_as_rows(loyalty: LoyaltyParameters) -> dict[str, str]:
    """The `shop_settings` rows that hold the Sultan Puan rules."""
    return {
        LOYALTY_ENABLED_KEY: "true" if loyalty.enabled else "false",
        LOYALTY_BASE_RATE_KEY: f"{loyalty.base_rate}",
        LOYALTY_BAND_MIN_KEY: f"{loyalty.band_min}",
        LOYALTY_BAND_MAX_KEY: f"{loyalty.band_max}",
        LOYALTY_CONVERSION_KEY: f"{loyalty.conversion_rate}",
        LOYALTY_ANNUAL_THRESHOLD_KEY: f"{loyalty.annual_spend_threshold}",
        LOYALTY_ANNUAL_BONUS_KEY: f"{loyalty.annual_bonus_points}",
        LOYALTY_ANNUAL_PERIOD_KEY: f"{loyalty.annual_period_days}",
        LOYALTY_ANNUAL_CEILING_KEY: f"{loyalty.annual_order_ceiling}",
    }


def _loyalty(rows: dict[str, str]) -> LoyaltyParameters:
    d = DEFAULT_LOYALTY
    band_min = _decimal(rows.get(LOYALTY_BAND_MIN_KEY), d.band_min, AMOUNT_MAX)
    band_max = _decimal(rows.get(LOYALTY_BAND_MAX_KEY), d.band_max, AMOUNT_MAX)
    if band_min > band_max:  # a band that cannot be met: the default band, whole
        band_min, band_max = d.band_min, d.band_max
    return LoyaltyParameters(
        enabled=_bool(rows.get(LOYALTY_ENABLED_KEY), d.enabled),
        base_rate=_decimal(rows.get(LOYALTY_BASE_RATE_KEY), d.base_rate, Decimal("1")),
        band_min=band_min,
        band_max=band_max,
        conversion_rate=_decimal(
            rows.get(LOYALTY_CONVERSION_KEY), d.conversion_rate, CONVERSION_MAX, CONVERSION_MIN
        ),
        annual_spend_threshold=_decimal(
            rows.get(LOYALTY_ANNUAL_THRESHOLD_KEY), d.annual_spend_threshold, AMOUNT_MAX, Decimal("0.01")
        ),
        annual_bonus_points=_int(rows.get(LOYALTY_ANNUAL_BONUS_KEY), d.annual_bonus_points, BONUS_POINTS_MAX),
        annual_period_days=_int(rows.get(LOYALTY_ANNUAL_PERIOD_KEY), d.annual_period_days, PERIOD_DAYS_MAX, 1),
        annual_order_ceiling=_decimal(rows.get(LOYALTY_ANNUAL_CEILING_KEY), d.annual_order_ceiling, AMOUNT_MAX),
    )


# ── Read / write ─────────────────────────────────────────────────────────────


async def load_parameters(db: AsyncSession) -> StoreParameters:
    """The live parameters, each falling back to its default on its own."""
    keys = [*parameters_as_rows(DEFAULT_PARAMETERS), *loyalty_as_rows(DEFAULT_LOYALTY), ISTANBUL_FREE_MIN_KEY]
    result = await db.execute(select(ShopSetting.key, ShopSetting.value).where(ShopSetting.key.in_(keys)))
    rows = {key: value for key, value in result}
    d = DEFAULT_PARAMETERS
    return StoreParameters(
        free_shipping_threshold=_decimal(
            rows.get(FREE_SHIPPING_THRESHOLD_KEY), d.free_shipping_threshold, AMOUNT_MAX
        ),
        coal_min_kg=_decimal(rows.get(COAL_MIN_KG_KEY), d.coal_min_kg, KG_MAX),
        coal_max_ratio=_decimal(rows.get(COAL_MAX_RATIO_KEY), d.coal_max_ratio, Decimal("1")),
        coal_rule_basis=_basis(rows.get(COAL_RULE_BASIS_KEY), d.coal_rule_basis),
        coal_min_quantity=_int(rows.get(COAL_MIN_QUANTITY_KEY), d.coal_min_quantity, QUANTITY_MAX),
        bank_transfer_tiers=_tiers(rows.get(BANK_TRANSFER_TIERS_KEY), d.bank_transfer_tiers),
        volume_tiers=_tiers(rows.get(VOLUME_TIERS_KEY), d.volume_tiers),
        loyalty=_loyalty(rows),
        istanbul_free_min=_decimal(rows.get(ISTANBUL_FREE_MIN_KEY), d.istanbul_free_min, ISTANBUL_FREE_MIN_MAX),
    )


async def save_parameters(db: AsyncSession, params: StoreParameters, admin_id=None) -> None:
    """Write every parameter. The caller validates and commits."""
    rows = {
        **parameters_as_rows(params),
        **loyalty_as_rows(params.loyalty),
        ISTANBUL_FREE_MIN_KEY: f"{params.istanbul_free_min.quantize(Decimal('0.01'))}",
    }
    for key, value in rows.items():
        await save_setting(db, key, value, admin_id)
