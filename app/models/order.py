import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PaymentMethod(str, enum.Enum):
    CREDIT_CARD = "CREDIT_CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    # An export order is not paid for at checkout: the freight, the customs and
    # the total are agreed afterwards, so there is no gateway to choose and this
    # is what the order records instead. Only an INTERNATIONAL account may send
    # it, and an order carrying it is a quote (see ``Order.is_quote``).
    QUOTE = "QUOTE"


class TransportMode(str, enum.Enum):
    """How an export shipment leaves the country, as the customer asks for it.

    The carriage is priced against this once the packages and the weight are
    known, which is why it is asked at the order rather than costed at it.
    """

    ROAD = "ROAD"
    AIR = "AIR"
    SEA = "SEA"


class OrderStatus(str, enum.Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    # Bank transfer placed; waiting for the money ("Havale Bekliyor").
    AWAITING_TRANSFER = "AWAITING_TRANSFER"
    # Card order placed; waiting for PayTR to confirm the payment ("Ödeme Bekliyor").
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    # Paid / approved by an admin.
    APPROVED = "APPROVED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"
    # PayTR reported the card payment failed (or it was never completed): the
    # reserved stock and points went back, as on a cancellation.
    PAYMENT_FAILED = "PAYMENT_FAILED"


# Statuses an admin can approve (mark paid) or cancel. A card order waiting for
# PayTR is among them: if the notification never arrives, an admin who sees the
# payment in the PayTR panel can still mark it paid.
PENDING_STATUSES = (OrderStatus.PENDING_APPROVAL, OrderStatus.AWAITING_TRANSFER, OrderStatus.AWAITING_PAYMENT)
# Orders that came to nothing: their stock went back, the points spent on them
# returned and the points earned on them are void.
VOID_STATUSES = (OrderStatus.CANCELLED, OrderStatus.PAYMENT_FAILED)


class ShippingMethod(str, enum.Enum):
    """How the order travels. Which of these a customer may pick depends on what
    kind of account they have — see ``METHODS_BY_ACCOUNT_TYPE`` in
    ``app.services.checkout_engine``."""

    # Yurtiçi Kargo ile adrese teslim. Offered to everyone domestic; a firm may
    # add its own Yurtiçi contract number (``shipping_contract_no``).
    YURTICI_DOOR = "YURTICI_DOOR"
    # Yurtiçi Kargo şube teslimatı — bireysel only. The parcel is addressed to
    # the customer ("adınıza gönderilir") unless somebody else is named.
    YURTICI_BRANCH = "YURTICI_BRANCH"
    # Nakliye ambarı: a freight depot delivers and collects its fee there. The
    # firm may name the depot (``freight_company_name`` / ``freight_phone``).
    NAKLIYE_AMBARI = "NAKLIYE_AMBARI"
    # The shop's own İstanbul run: free, and offered only to a firm whose
    # registered address is in İstanbul.
    ISTANBUL_FREE = "ISTANBUL_FREE"
    # Export: freight and customs are quoted after the order, not at checkout.
    INTERNATIONAL_FREIGHT = "INTERNATIONAL_FREIGHT"


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
        Enum(ShippingMethod),
        nullable=False,
        default=ShippingMethod.YURTICI_DOOR,
        server_default="YURTICI_DOOR",
    )
    shipping_payer: Mapped[str] = mapped_column(
        Enum(ShippingPayer), nullable=False, default=ShippingPayer.SHOP, server_default="SHOP"
    )
    # When the buyer ticked "gönderim ücreti alıcı tarafından ödenecektir"; null
    # on orders that ship free and on orders placed before the box existed.
    shipping_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {full_name, phone, country, city, district, line, postal_code}; null on
    # orders from before addresses existed.
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)
    # "Başka biri teslim alacak": who signs for the parcel, when it is not the
    # customer. Both or neither.
    receiver_name: Mapped[str | None] = mapped_column(String(150))
    receiver_phone: Mapped[str | None] = mapped_column(String(30))
    # The buyer's own Yurtiçi Kargo agreement, so the carriage is billed to it
    # ("size ait anlaşma varsa kodunuzu girebilirsiniz"). Optional, and only on
    # the two Yurtiçi methods.
    shipping_contract_no: Mapped[str | None] = mapped_column(String(50))
    # The nakliye ambarı the firm wants the goods handed to, when it has one in
    # mind. Optional, NAKLIYE_AMBARI only.
    freight_company_name: Mapped[str | None] = mapped_column(String(150))
    freight_phone: Mapped[str | None] = mapped_column(String(30))
    # "Şubeme istiyorum": the firm's own branch this order goes to, by the name
    # the firm calls it ("Kadıköy Şubesi"). Set only when the delivery address
    # is a branch rather than the registered invoice address, and it is what
    # allows that address to be somewhere else.
    shipping_branch_name: Mapped[str | None] = mapped_column(String(150))
    # "Faturayı firmaya/şirkete istiyorum": a personal order invoiced to a firm.
    # The three fields below are filled only then, and are what the invoice is
    # made out to — the account's own registered details are not touched.
    invoice_to_company: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    invoice_company_title: Mapped[str | None] = mapped_column(String(200))
    # "Firma Adı": the name the firm trades under, when it differs from the
    # registered ünvan above. Printed beside it, never instead of it.
    invoice_trade_name: Mapped[str | None] = mapped_column(String(200))
    invoice_tax_office: Mapped[str | None] = mapped_column(String(100))
    invoice_tax_number: Mapped[str | None] = mapped_column(String(11))
    # The firm's official billing address, frozen onto the order the way
    # `shipping_address` is: {city, district, line}. The invoice is a document
    # about the day it was issued, so it does not follow the account afterwards.
    invoice_address: Mapped[dict | None] = mapped_column(JSONB)
    # "Kart bilgilerimi sonraki alışverişlerim için güvenle kaydet": the buyer's
    # wish, recorded here. No card data is ever stored by us — the payment
    # provider keeps the token.
    save_card: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # An order request rather than an order: an export customer builds a cart
    # at export prices and asks for it to be confirmed. Nothing is charged and
    # no stock moves — the quantities are what is being quoted, not what is
    # being taken out of the warehouse — so these never reach the sales
    # figures as a sale until an admin turns one into one.
    is_quote: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # "Delivery information": road, air or sea, on an export request only.
    transport_mode: Mapped[str | None] = mapped_column(
        Enum(TransportMode), nullable=True
    )
    # "Sipariş Notu": whatever the customer wanted to tell us when they
    # ordered — a delivery window, a missing invoice detail, a request. Free
    # text, kept as typed (line breaks and all) and never acted on by code.
    order_note: Mapped[str | None] = mapped_column(Text)
    # When both agreements (mesafeli satış, şartlar ve kurallar) were accepted.
    # Null only on orders placed before the gate existed; no order can be
    # placed without them now.
    agreements_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
