import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderStatus, PaymentMethod


class OrderItemCreate(BaseModel):
    variant_id: uuid.UUID
    quantity: int
    unit_price: Decimal


class OrderCreate(BaseModel):
    payment_method: PaymentMethod
    items: list[OrderItemCreate]


class CartItemInput(BaseModel):
    variant_id: uuid.UUID
    # Must be positive: a negative quantity would lower the total and add stock.
    quantity: int = Field(gt=0)


class CheckoutRequest(BaseModel):
    """Checkout / cart-calculate request — prices resolved server-side.

    The customer is always the authenticated user; there is deliberately no user_id.
    """

    payment_method: PaymentMethod
    items: list[CartItemInput] = Field(min_length=1)
    points_to_redeem: int = Field(default=0, ge=0)


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    variant_id: uuid.UUID
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_number: str
    user_id: uuid.UUID
    total_amount: Decimal
    shipping_cost: Decimal
    discount_amount: Decimal
    payment_method: PaymentMethod
    status: OrderStatus
    tracking_code: str | None
    created_at: datetime
    items: list[OrderItemRead] = []
