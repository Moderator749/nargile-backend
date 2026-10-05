import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

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
    quantity: int


class CheckoutRequest(BaseModel):
    """Checkout / cart-calculate request — prices resolved server-side."""
    user_id: uuid.UUID
    payment_method: PaymentMethod
    items: list[CartItemInput]
    points_to_redeem: int = 0


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
