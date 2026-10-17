import uuid
from decimal import Decimal

from typing import Literal

from pydantic import BaseModel, Field

from app.models.order import PaymentMethod


class CartItem(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(gt=0)


class CartCalculateRequest(BaseModel):
    """The customer is always the authenticated user — never taken from the body."""

    payment_method: PaymentMethod
    items: list[CartItem] = Field(min_length=1)


class CartItemBreakdown(BaseModel):
    variant_id: uuid.UUID
    product_id: uuid.UUID | None = None
    # For the link to the product's page, /product/<slug>.
    product_slug: str | None = None
    sku: str
    product_name: str
    # The "Renk" of the variant in the cart; null on a colourless product.
    color_name: str | None = None
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    tags: list[str]
    image_url: str | None = None
    # Percent; `unit_price` and `line_total` include it.
    vat_rate: Decimal = Decimal("0")
    # A B2B line (bayi, toptan or kafe) whose product has that list's havale
    # switch on — set whatever the payment, so the cart can say what a transfer
    # takes off.
    havale_eligible: bool = False
    # The havale discount on this line itself (VAT-inclusive); 0 unless it is a
    # havale_eligible line paid by transfer. Included in `discount_amount`.
    havale_discount: Decimal = Decimal("0.00")
    # How many the buyer may have (their own list's quota): a
    # line above it cannot be ordered, and the cart says so before checkout.
    available_quantity: int = 0


class ShippingBreakdown(BaseModel):
    """Who pays the carriage and why, so the cart can explain it.

    Carriage is never added to the order (`cargo_cost` is always 0): either the
    shop covers it, or the shipment goes "karşı ödemeli" and the buyer pays the
    carrier on delivery.
    """

    # The shop pays the carrier.
    free: bool
    # "Karşı ödemeli": paid to the carrier/courier on delivery. Requires the
    # buyer's consent at checkout.
    receiver_pays: bool
    # coal_weight: the "%50 Kuralı" — a coal-dominant cart (> coal_max_ratio)
    # below the minimum: coal_min_kg, or coal_min_quantity when coal_rule_basis
    # is "quantity".
    # freight: nakliye ambarı, which collects its own fee from the receiver.
    # international: freight and customs, quoted after the order.
    # tier_price: a B2B account, whose price list does not include carriage, so
    # the free-shipping threshold does not apply to it at all.
    reason: Literal[
        "free", "below_threshold", "coal_weight", "freight", "international", "tier_price"
    ]
    free_threshold: Decimal
    coal_total: Decimal
    coal_ratio: Decimal  # 0..1
    coal_max_ratio: Decimal
    coal_weight_kg: Decimal
    coal_min_kg: Decimal
    coal_weight_unknown: bool
    coal_rule_basis: Literal["kg", "quantity"]
    coal_quantity: int
    coal_min_quantity: int


class VolumeTier(BaseModel):
    threshold: Decimal
    rate: Decimal


class CartCalculateResponse(BaseModel):
    subtotal: Decimal
    # Whether this account gets the retail promotions (Sultan Puan, ciro and havale
    # discounts). False means the amounts below are zero by rule: a B2B account
    # pays its tier price net. See app.services.perks.
    perks_eligible: bool = True
    cargo_cost: Decimal
    shipping: ShippingBreakdown
    # Volume discount ("ciro indirimi"), on the subtotal.
    volume_discount_rate: Decimal
    volume_discount_amount: Decimal
    # The next volume tier the cart hasn't reached (for "₺X more for 8%"); None at the top.
    volume_next_tier: VolumeTier | None
    # Bank transfer (havale/EFT) discount: for retail on what's left after the
    # volume discount; for a bayi the sum of its lines' `havale_discount`.
    discount_rate: Decimal
    discount_amount: Decimal
    points_discount: Decimal
    net_total: Decimal
    # The VAT inside the merchandise, for a business invoice: the lines before
    # VAT, the line discounts before VAT (a bayi's havale), the VAT on what the
    # lines come to after them, and the rates involved. subtotal_excl_tax −
    # line_discount_excl_tax + vat_total is what the lines come to. Not for a
    # retail cart, whose discounts are cart-wide.
    subtotal_excl_tax: Decimal = Decimal("0.00")
    line_discount_excl_tax: Decimal = Decimal("0.00")
    vat_total: Decimal = Decimal("0.00")
    vat_rates: list[Decimal] = Field(default_factory=list)
    loyalty_points_to_earn: int
    payment_method: PaymentMethod
    items: list[CartItemBreakdown]
