import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.order import OrderStatus, PaymentMethod, ShippingMethod, ShippingPayer
from app.schemas.address import ISTANBUL, ShippingAddress


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
    shipping_method: ShippingMethod = ShippingMethod.CARGO
    # Optional for price previews; required to place an order.
    shipping_address: ShippingAddress | None = None
    # "Gönderim ücreti teslimat sırasında alıcı tarafından ödenecektir. Okudum,
    # onayladım." Required to check out whenever the shop does not cover carriage;
    # ignored on price previews.
    shipping_consent: bool = False

    @model_validator(mode="after")
    def _courier_is_istanbul_only(self) -> "CheckoutRequest":
        if (
            self.shipping_method == ShippingMethod.COURIER_ISTANBUL
            and self.shipping_address is not None
            and self.shipping_address.city != ISTANBUL
        ):
            raise ValueError("İstanbul içi kurye yalnızca İstanbul adreslerine gönderilir")
        return self


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
    shipping_method: ShippingMethod = ShippingMethod.CARGO
    shipping_payer: ShippingPayer = ShippingPayer.SHOP
    shipping_consent_at: datetime | None = None
    shipping_address: dict | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    created_at: datetime
    items: list[OrderItemRead] = []
