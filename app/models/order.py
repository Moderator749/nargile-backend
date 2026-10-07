import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PaymentMethod(str, enum.Enum):
    CREDIT_CARD = "CREDIT_CARD"
    BANK_TRANSFER = "BANK_TRANSFER"


class OrderStatus(str, enum.Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    # Bank transfer placed; waiting for the money ("Havale Bekliyor").
    AWAITING_TRANSFER = "AWAITING_TRANSFER"
    # Paid / approved by an admin.
    APPROVED = "APPROVED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"


# Statuses an admin can approve (mark paid) or cancel.
PENDING_STATUSES = (OrderStatus.PENDING_APPROVAL, OrderStatus.AWAITING_TRANSFER)


class ShippingMethod(str, enum.Enum):
    CARGO = "CARGO"
    # Same-day Istanbul courier, 10:00–20:00; the buyer pays the courier on delivery.
    COURIER_ISTANBUL = "COURIER_ISTANBUL"


class ShippingPayer(str, enum.Enum):
    """Who pays the carriage. The shop never charges it on the order either way.

    SHOP: the free-shipping rules were met, so the shop pays the carrier.
    RECEIVER: "karşı ödemeli" — the buyer pays the carrier on delivery, which is
    why the order's shipping_cost is always 0.00.
    """

    SHOP = "SHOP"
    RECEIVER = "RECEIVER"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_number: Mapped[str] = mapped_column(
        String(30), unique=True, nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    shipping_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    payment_method: Mapped[str] = mapped_column(Enum(PaymentMethod), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.PENDING_APPROVAL, nullable=False
    )
    tracking_code: Mapped[str | None] = mapped_column(String(100))
    shipping_method: Mapped[str] = mapped_column(
        Enum(ShippingMethod), nullable=False, default=ShippingMethod.CARGO, server_default="CARGO"
    )
    shipping_payer: Mapped[str] = mapped_column(
        Enum(ShippingPayer), nullable=False, default=ShippingPayer.SHOP, server_default="SHOP"
    )
    # When the buyer ticked "gönderim ücreti alıcı tarafından ödenecektir"; null
    # on orders that ship free and on orders placed before the box existed.
    shipping_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {full_name, phone, city, district, line}; null on orders from before addresses existed.
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)
    # When an admin approved / marked it paid (loyalty is granted then).
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
