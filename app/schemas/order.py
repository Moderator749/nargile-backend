import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.tr_identity import normalize_vkn
from app.models.order import OrderStatus, PaymentMethod, ShippingMethod, ShippingPayer
from app.schemas.address import ShippingAddress, valid_contact_phone

# Both Yurtiçi Kargo options; the agreement number belongs to the carrier, not
# to one of its two ways of delivering.
YURTICI = (ShippingMethod.YURTICI_DOOR, ShippingMethod.YURTICI_BRANCH)


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
    shipping_method: ShippingMethod = ShippingMethod.YURTICI_DOOR
    # Optional for price previews; required to place an order.
    shipping_address: ShippingAddress | None = None
    # "Gönderim ücreti teslimat sırasında alıcı tarafından ödenecektir. Okudum,
    # onayladım." Required to check out whenever the shop does not cover carriage;
    # ignored on price previews.
    shipping_consent: bool = False
    # "Başka biri teslim alacak": both or neither. Who signs for the parcel is
    # not who ordered it, so the carrier needs a name and a number of their own.
    receiver_name: str | None = Field(default=None, max_length=150)
    receiver_phone: str | None = Field(default=None, max_length=30)
    # "Size ait anlaşma varsa kodunuzu girebilirsiniz": the buyer's own Yurtiçi
    # Kargo agreement, so the carriage is billed to it. Kept on either Yurtiçi
    # method, dropped on the others.
    shipping_contract_no: str | None = Field(default=None, max_length=50)
    # "Ambar bilgisi varsa ekleyiniz": the nakliye ambarı to hand the goods to.
    freight_company_name: str | None = Field(default=None, max_length=150)
    freight_phone: str | None = Field(default=None, max_length=30)
    # "Şubeme istiyorum": the branch this order is for, named by the firm. It is
    # what tells the shop — and ``app.services.delivery`` — that the address
    # below is deliberately not the registered invoice address.
    shipping_branch_name: str | None = Field(default=None, max_length=150)
    # "Faturayı firmaya/şirkete istiyorum": the invoice is made out to a firm,
    # which then has to say which one. The account's own details are untouched.
    invoice_to_company: bool = False
    invoice_company_title: str | None = Field(default=None, max_length=200)
    invoice_tax_office: str | None = Field(default=None, max_length=100)
    invoice_tax_number: str | None = Field(default=None, max_length=20)
    # "Kart bilgilerimi sonraki alışverişlerim için güvenle kaydet". Recorded as
    # a wish; the card itself is only ever held by the payment provider.
    save_card: bool = False
    # The two agreements. Both are required to place an order and ignored on a
    # price preview, like `shipping_consent` — the engine is what refuses.
    distance_sales_accepted: bool = False
    terms_accepted: bool = False

    @model_validator(mode="after")
    def _deliverable(self) -> "CheckoutRequest":
        # The chosen method is the authority: what belongs to another one is
        # dropped rather than refused, so a customer who types an ambar and then
        # changes their mind is not told off for it.
        if self.shipping_method not in YURTICI:
            self.shipping_contract_no = None
        else:
            self.shipping_contract_no = (self.shipping_contract_no or "").strip() or None

        if self.shipping_method != ShippingMethod.NAKLIYE_AMBARI:
            self.freight_company_name = None
            self.freight_phone = None
        else:
            self.freight_company_name = " ".join((self.freight_company_name or "").split()) or None
            given = (self.freight_phone or "").strip()
            self.freight_phone = valid_contact_phone(given) if given else None

        # A branch is a destination, not a carrier, so it survives whichever
        # domestic method takes it there. An export order has no branches.
        self.shipping_branch_name = " ".join((self.shipping_branch_name or "").split()) or None
        if self.shipping_method is ShippingMethod.INTERNATIONAL_FREIGHT:
            self.shipping_branch_name = None
        elif self.shipping_branch_name is not None and len(self.shipping_branch_name) < 2:
            raise ValueError("Şube adını girin")

        self.receiver_name = " ".join((self.receiver_name or "").split()) or None
        given = (self.receiver_phone or "").strip()
        if self.receiver_name or given:
            if not self.receiver_name or len(self.receiver_name) < 2:
                raise ValueError("Teslim alacak kişinin adını girin")
            self.receiver_phone = valid_contact_phone(given)
        else:
            self.receiver_phone = None
        return self

    @model_validator(mode="after")
    def _invoiceable(self) -> "CheckoutRequest":
        """An invoice to a firm has to say which firm.

        A mistyped VKN is only discovered when the invoice is rejected, so it is
        checked here. Details left over from an unticked box are dropped rather
        than refused.
        """
        if not self.invoice_to_company:
            self.invoice_company_title = None
            self.invoice_tax_office = None
            self.invoice_tax_number = None
            return self

        self.invoice_company_title = " ".join((self.invoice_company_title or "").split())
        self.invoice_tax_office = " ".join((self.invoice_tax_office or "").split())
        if len(self.invoice_company_title) < 2:
            raise ValueError("Fatura için firma ünvanını girin")
        if len(self.invoice_tax_office) < 2:
            raise ValueError("Fatura için vergi dairesini girin")
        self.invoice_tax_number = normalize_vkn(self.invoice_tax_number or "")
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
    shipping_method: ShippingMethod = ShippingMethod.YURTICI_DOOR
    shipping_payer: ShippingPayer = ShippingPayer.SHOP
    shipping_consent_at: datetime | None = None
    shipping_address: dict | None = None
    # Set when somebody other than the customer collects the parcel.
    receiver_name: str | None = None
    receiver_phone: str | None = None
    # A firm's Yurtiçi agreement, and the nakliye ambarı it named.
    shipping_contract_no: str | None = None
    freight_company_name: str | None = None
    freight_phone: str | None = None
    # The firm's own branch this order was sent to, when it was not the
    # registered invoice address.
    shipping_branch_name: str | None = None
    # Who the invoice is made out to, when it is not the customer themselves.
    invoice_to_company: bool = False
    invoice_company_title: str | None = None
    invoice_tax_office: str | None = None
    invoice_tax_number: str | None = None
    save_card: bool = False
    # When the two agreements were accepted; no order is placed without them.
    agreements_accepted_at: datetime | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    created_at: datetime
    items: list[OrderItemRead] = []
