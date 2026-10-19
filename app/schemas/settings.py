"""The shop settings the panel edits, and what the storefront is told."""

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.shop_settings import ISTANBUL_FREE_MIN_DEFAULT, ISTANBUL_FREE_MIN_MAX
from app.services.store_parameters import (
    AMOUNT_MAX,
    BONUS_POINTS_MAX,
    CONVERSION_MAX,
    CONVERSION_MIN,
    KG_MAX,
    MAX_TIERS,
    PERIOD_DAYS_MAX,
    QUANTITY_MAX,
    CoalRuleBasis,
    LoyaltyParameters,
    StoreParameters,
    Tier,
)


class ShippingSettingsRead(BaseModel):
    """Read without a login: the cart has to say what the limit is before the
    customer picks the option it belongs to."""

    # "İstanbul İçi B2B Ücretsiz Teslimat Alt Limiti (TL)".
    istanbul_free_min_subtotal: Decimal = ISTANBUL_FREE_MIN_DEFAULT


# ── Parametreler ─────────────────────────────────────────────────────────────


class TierSchema(BaseModel):
    """One rung of a discount ladder: from `threshold` TRY, `rate` (0..1) off."""

    threshold: Decimal = Field(gt=0, le=AMOUNT_MAX, decimal_places=2)
    # A fraction, as the cart reports rates: 0.05 is %5.
    rate: Decimal = Field(ge=0, le=1, decimal_places=4)


class LoyaltySchema(BaseModel):
    """Sultan Puan rules; see ``LoyaltyParameters``."""

    enabled: bool
    # A fraction: 0.10 is %10 of the order's subtotal.
    base_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    band_min: Decimal = Field(ge=0, le=AMOUNT_MAX, decimal_places=2)
    band_max: Decimal = Field(ge=0, le=AMOUNT_MAX, decimal_places=2)
    # TRY per point.
    conversion_rate: Decimal = Field(ge=CONVERSION_MIN, le=CONVERSION_MAX, decimal_places=2)
    annual_spend_threshold: Decimal = Field(gt=0, le=AMOUNT_MAX, decimal_places=2)
    annual_bonus_points: int = Field(ge=0, le=BONUS_POINTS_MAX)
    annual_period_days: int = Field(ge=1, le=PERIOD_DAYS_MAX)
    # 0 counts every order toward the annual target.
    annual_order_ceiling: Decimal = Field(ge=0, le=AMOUNT_MAX, decimal_places=2)

    @model_validator(mode="after")
    def _band(self) -> "LoyaltySchema":
        if self.band_min > self.band_max:
            raise ValueError("Puan kazanım aralığının alt sınırı üst sınırından büyük olamaz")
        return self

    @classmethod
    def of(cls, loyalty: LoyaltyParameters) -> "LoyaltySchema":
        return cls(**loyalty.__dict__)

    def to_parameters(self) -> LoyaltyParameters:
        return LoyaltyParameters(
            **{
                **self.model_dump(),
                "band_min": self.band_min.quantize(Decimal("0.01")),
                "band_max": self.band_max.quantize(Decimal("0.01")),
                "conversion_rate": self.conversion_rate.quantize(Decimal("0.01")),
                "annual_spend_threshold": self.annual_spend_threshold.quantize(Decimal("0.01")),
                "annual_order_ceiling": self.annual_order_ceiling.quantize(Decimal("0.01")),
            }
        )


class StoreParametersSchema(BaseModel):
    """The checkout's commercial rules (see ``app.services.store_parameters``).

    Read without a login, because the storefront states them to everyone (the
    free shipping bar, the havale hint); written by an admin, whole.
    """

    free_shipping_threshold: Decimal = Field(ge=0, le=AMOUNT_MAX, decimal_places=2)
    coal_min_kg: Decimal = Field(ge=0, le=KG_MAX, decimal_places=3)
    # 0..1: coal worth more than this share of the subtotal triggers the "%50 Kuralı".
    coal_max_ratio: Decimal = Field(ge=0, le=1, decimal_places=4)
    # Which minimum the rule applies: coal_min_kg ("kg") or coal_min_quantity ("quantity").
    coal_rule_basis: CoalRuleBasis = "kg"
    coal_min_quantity: int = Field(default=20, ge=0, le=QUANTITY_MAX)
    bank_transfer_tiers: list[TierSchema] = Field(max_length=MAX_TIERS)
    volume_tiers: list[TierSchema] = Field(max_length=MAX_TIERS)
    loyalty: LoyaltySchema
    # "İstanbul İçi B2B Ücretsiz Teslimat Alt Limiti (TL)"; zero puts every
    # İstanbul order on the van for free.
    istanbul_free_min_subtotal: Decimal = Field(ge=0, le=ISTANBUL_FREE_MIN_MAX, decimal_places=2)

    @field_validator("bank_transfer_tiers", "volume_tiers")
    @classmethod
    def _ladder(cls, tiers: list[TierSchema]) -> list[TierSchema]:
        if len({t.threshold for t in tiers}) != len(tiers):
            raise ValueError("Aynı tutar sınırı iki kez girilemez")
        return sorted(tiers, key=lambda t: t.threshold)

    @classmethod
    def of(cls, params: StoreParameters) -> "StoreParametersSchema":
        return cls(
            free_shipping_threshold=params.free_shipping_threshold,
            coal_min_kg=params.coal_min_kg,
            coal_max_ratio=params.coal_max_ratio,
            coal_rule_basis=params.coal_rule_basis,
            coal_min_quantity=params.coal_min_quantity,
            bank_transfer_tiers=[TierSchema(threshold=t.threshold, rate=t.rate) for t in params.bank_transfer_tiers],
            volume_tiers=[TierSchema(threshold=t.threshold, rate=t.rate) for t in params.volume_tiers],
            loyalty=LoyaltySchema.of(params.loyalty),
            istanbul_free_min_subtotal=params.istanbul_free_min,
        )

    def to_parameters(self) -> StoreParameters:
        def ladder(tiers: list[TierSchema]) -> tuple[Tier, ...]:
            return tuple(Tier(t.threshold.quantize(Decimal("0.01")), t.rate) for t in tiers)

        return StoreParameters(
            free_shipping_threshold=self.free_shipping_threshold.quantize(Decimal("0.01")),
            coal_min_kg=self.coal_min_kg,
            coal_max_ratio=self.coal_max_ratio,
            coal_rule_basis=self.coal_rule_basis,
            coal_min_quantity=self.coal_min_quantity,
            bank_transfer_tiers=ladder(self.bank_transfer_tiers),
            volume_tiers=ladder(self.volume_tiers),
            loyalty=self.loyalty.to_parameters(),
            istanbul_free_min=self.istanbul_free_min_subtotal.quantize(Decimal("0.01")),
        )
