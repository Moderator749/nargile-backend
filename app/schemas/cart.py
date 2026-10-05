import uuid
from decimal import Decimal

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
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    tags: list[str]


class CartCalculateResponse(BaseModel):
    subtotal: Decimal
    cargo_cost: Decimal
    discount_rate: Decimal
    discount_amount: Decimal
    points_discount: Decimal
    net_total: Decimal
    loyalty_points_to_earn: int
    payment_method: PaymentMethod
    items: list[CartItemBreakdown]
