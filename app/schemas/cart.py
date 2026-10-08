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
    sku: str
    product_name: str
    # The "Renk" of the variant in the cart; null on a colourless product.
    color_name: str | None = None
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    tags: list[str]
    image_url: str | None = None


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
    # coal_weight: a coal-dominant cart (> coal_max_ratio) below coal_min_kg.
    # courier: Istanbul courier, paid to the courier on delivery, by distance.
    reason: Literal["free", "below_threshold", "coal_weight", "courier"]
    free_threshold: Decimal
    coal_total: Decimal
    coal_ratio: Decimal  # 0..1
    coal_max_ratio: Decimal
    coal_weight_kg: Decimal
    coal_min_kg: Decimal
    coal_weight_unknown: bool


class VolumeTier(BaseModel):
    threshold: Decimal
    rate: Decimal


class CartCalculateResponse(BaseModel):
    subtotal: Decimal
    cargo_cost: Decimal
    shipping: ShippingBreakdown
    # Volume discount ("ciro indirimi"), on the subtotal.
    volume_discount_rate: Decimal
    volume_discount_amount: Decimal
    # The next volume tier the cart hasn't reached (for "₺X more for 8%"); None at the top.
    volume_next_tier: VolumeTier | None
    # Bank transfer (havale/EFT) discount, on what's left after the volume discount.
    discount_rate: Decimal
    discount_amount: Decimal
    points_discount: Decimal
    net_total: Decimal
    loyalty_points_to_earn: int
    payment_method: PaymentMethod
    items: list[CartItemBreakdown]
