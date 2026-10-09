"""Checkout engine implementing Turhan Abi's commercial rules.

Four of these rules are retail promotions and reach only a bireysel account on
the retail price list — the volume discount, the havale discount, ParaPuan, and
free shipping.
A kafe, a shop or an export customer buys at a tier price those were never
costed on top of, so it pays that price net, whatever it pays with and whatever
it has in its wallet. ``app.services.perks`` is the one place that decides it.

Business rules (all tiers are judged on the merchandise subtotal):
- Volume discount ("ciro indirimi"): 5% for 30,000-49,999.99 TRY, 8% for >= 50,000
  — retail only
- Bank transfer discount: 5% for 1-29,999.99 TRY, 7% for >= 30,000, applied
  sequentially: on the amount left after the volume discount — retail only
- ParaPuan/Kumbarapuan: granted when an order is paid (see app.services.wallet)
  — retail only
- ParaPuan redemption: 1 point = 1.00 TRY, capped at the discounted subtotal
  — retail only
- Shipping method: each kind of account is offered its own (see
  ``METHODS_BY_ACCOUNT_TYPE``); a nakliye ambarı and an export shipment are
  always paid by the receiver, since the shop cannot quote either at checkout
- Free shipping is retail only: a B2B account buys at a tier price that does not
  include carriage, so its shipments go "karşı ödemeli" whatever the cart is
  worth and whichever carrier takes them
- Cargo: the shop never charges carriage on the order — the cost is always ₺0.
  Free from 3,000 TRY means the shop pays the carrier; below that the shipment
  goes "karşı ödemeli" and the buyer pays the carrier on delivery (which the
  buyer confirms with a checkbox at checkout). The 20 kg coal minimum applies
  only to coal-dominant carts (coal, i.e. the Kömürler category, worth more than
  50% of the subtotal): those ship on the shop only from 20 kg of coal (unknown
  weights count as 0 kg). Where hardware dominates, its margin absorbs the freight.
- The shop's own İstanbul run is free for a firm whose registered address is in
  İstanbul, from a subtotal the admin panel sets ("İstanbul içi 20000 ve üzeri
  alışverişlerde ücretsiz teslimat"; see ``app.services.shop_settings``).
  Below that limit the van is not offered at all
- Flash sales: a live sale price replaces the segment price when lower; once
  the sale ends the segment price applies again
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import (
    CurrencyType,
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod, ShippingMethod, ShippingPayer
from app.models.user import AccountType, SegmentType, User
from app.services.categories import COAL_CATEGORY_SLUG, category_subtree
from app.services.delivery import delivers_in_istanbul
from app.services.perks import PERKS_REFUSED, retail_perks
from app.services.shop_settings import ISTANBUL_FREE_MIN_DEFAULT, istanbul_free_minimum
from app.services.exchange_rates import Rates, RatesUnavailable, get_rates
from app.services.flash_sales import discounted_price, live_flash_sales
from app.services.loyalty_service import get_user_loyalty_balance
from app.services.wallet import parapuan_for
from app.services.pricing_engine import calculate_line_total, pick_price_row, to_try

# ── Constants ────────────────────────────────────────────────────────────────

FREE_SHIPPING_THRESHOLD = Decimal("3000.00")
COAL_MAX_RATIO = Decimal("0.50")  # above this share of the subtotal, the 20 kg coal minimum applies
COAL_MIN_FREE_KG = Decimal("20")  # coal ships free only from this weight
VOLUME_LOW_THRESHOLD = Decimal("30000.00")
VOLUME_LOW_RATE = Decimal("0.05")
VOLUME_HIGH_THRESHOLD = Decimal("50000.00")
VOLUME_HIGH_RATE = Decimal("0.08")
VOLUME_TIERS = ((VOLUME_LOW_THRESHOLD, VOLUME_LOW_RATE), (VOLUME_HIGH_THRESHOLD, VOLUME_HIGH_RATE))

BANK_TRANSFER_LOW_CEILING = Decimal("29999.99")
BANK_TRANSFER_LOW_RATE = Decimal("0.05")
BANK_TRANSFER_HIGH_THRESHOLD = Decimal("30000.00")
BANK_TRANSFER_HIGH_RATE = Decimal("0.07")

POINTS_TO_TRY = Decimal("1.00")  # 1 ParaPuan = 1.00 TRY


def try_amount(value: Decimal) -> str:
    """An amount the way it is written in Turkish: 20.000,00."""
    return f"{value:,.2f}".translate(str.maketrans(",.", ".,"))


# One quota per segment, as the product sheet defines them (Bayi / Toptan / Kafe
# / Perakende). B2B_STOCK is the Toptan quota, named before the split.
WAREHOUSE_BY_SEGMENT = {
    SegmentType.CAFE: WarehouseType.CAFE_STOCK,
    SegmentType.WHOLESALE: WarehouseType.B2B_STOCK,
    SegmentType.DEALER: WarehouseType.DEALER_STOCK,
}


def warehouse_for_segment(code: SegmentType | None) -> WarehouseType:
    """The stock quota a segment buys from; RETAIL_STOCK when it has no segment."""
    return WAREHOUSE_BY_SEGMENT.get(code, WarehouseType.RETAIL_STOCK)


# What each kind of account may be shipped by. A bireysel parcel goes to a door
# or to a branch. A firm's goes to its door by Yurtiçi, to a nakliye ambarı, or
# — in İstanbul only, and then free — on the shop's own van. An export order
# goes one way, and its cost is quoted afterwards.
_BUSINESS_METHODS = frozenset({
    ShippingMethod.YURTICI_DOOR,
    ShippingMethod.NAKLIYE_AMBARI,
    ShippingMethod.ISTANBUL_FREE,
})
METHODS_BY_ACCOUNT_TYPE: dict[AccountType, frozenset[ShippingMethod]] = {
    AccountType.INDIVIDUAL: frozenset({ShippingMethod.YURTICI_DOOR, ShippingMethod.YURTICI_BRANCH}),
    AccountType.CAFE: _BUSINESS_METHODS,
    AccountType.TOBACCO_SHOP: _BUSINESS_METHODS,
    AccountType.SOLE_PROPRIETOR: _BUSINESS_METHODS,
    AccountType.COMPANY: _BUSINESS_METHODS,
    AccountType.INTERNATIONAL: frozenset({ShippingMethod.INTERNATIONAL_FREIGHT}),
}


def shipping_methods_for(account_type: AccountType | str) -> frozenset[ShippingMethod]:
    """The methods this account may pick from; a door delivery if we have never
    heard of its type.

    ``ISTANBUL_FREE`` is in a firm's set but is offered only where the van goes:
    ``resolve_cart`` checks the registered address as well.
    """
    return METHODS_BY_ACCOUNT_TYPE.get(
        AccountType(account_type), frozenset({ShippingMethod.YURTICI_DOOR})
    )


# ── Resolved cart item (after DB lookup) ─────────────────────────────────────

@dataclass
class ResolvedCartItem:
    variant: ProductVariant
    product: Product
    unit_price: Decimal
    quantity: int
    line_total: Decimal = field(init=False)
    tags: list[str] = field(init=False)
    # In the Kömürler category (or a subcategory).
    is_coal: bool = False
    # Charged at the product's "Fırsat" price, so the deal's stock is spent.
    offer_applied: bool = False
    # kg per unit; None = not recorded.
    unit_weight_kg: Decimal | None = None

    def __post_init__(self) -> None:
        self.line_total = calculate_line_total(self.unit_price, self.quantity)
        self.tags = self.product.tags or []

# ── Shipping quote ───────────────────────────────────────────────────────────

ShippingReason = Literal[
    "free", "below_threshold", "coal_weight", "freight", "international", "tier_price"
]


@dataclass(frozen=True)
class ShippingQuote:
    # Always 0.00: carriage is never added to the order. `reason` says who pays it.
    cost: Decimal
    reason: ShippingReason  # who pays the carriage and why, for the cart's explanation
    coal_total: Decimal
    coal_ratio: Decimal  # coal_total / subtotal, 0..1
    coal_weight_kg: Decimal  # known coal weight in the cart
    coal_weight_unknown: bool  # some coal in the cart has no recorded weight

    @property
    def free(self) -> bool:
        """The shop pays the carrier: nothing is collected from the buyer."""
        return self.reason == "free"

    @property
    def receiver_pays(self) -> bool:
        """"Karşı ödemeli": the buyer pays the carrier (or courier) on delivery."""
        return not self.free

    @property
    def payer(self) -> ShippingPayer:
        return ShippingPayer.SHOP if self.free else ShippingPayer.RECEIVER


# ── Cart calculation result ──────────────────────────────────────────────────

@dataclass
class CartResult:
    items: list[ResolvedCartItem]
    subtotal: Decimal
    cargo_cost: Decimal
    shipping: ShippingQuote
    volume_discount_rate: Decimal
    volume_discount_amount: Decimal
    # Bank transfer (havale/EFT) discount.
    discount_rate: Decimal
    discount_amount: Decimal
    points_discount: Decimal
    net_total: Decimal
    loyalty_points_to_earn: int
    points_redeemed: int
    payment_method: PaymentMethod
    # Whether this account gets the retail promotions at all. False means every
    # discount above is zero by rule, not by arithmetic.
    perks_eligible: bool = True


# ── Engine ───────────────────────────────────────────────────────────────────

class CheckoutEngine:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Public API ───────────────────────────────────────────────────────

    async def resolve_cart(
        self,
        user: User,
        variant_quantities: dict[uuid.UUID, int],
        payment_method: PaymentMethod,
        points_to_redeem: int = 0,
        shipping_method: ShippingMethod = ShippingMethod.YURTICI_DOOR,
    ) -> CartResult:
        """Resolve raw cart items into a full calculation with all business rules."""
        segment_code = user.segment.code if user.segment else None
        if shipping_method not in shipping_methods_for(user.account_type):
            raise ValueError("Bu kargo seçeneği hesabınız için geçerli değil")
        if shipping_method is ShippingMethod.ISTANBUL_FREE and not await delivers_in_istanbul(
            self._db, user
        ):
            raise ValueError(
                "İstanbul içi ücretsiz teslimat yalnızca İstanbul'daki kayıtlı adresler için geçerlidir"
            )
        perks = retail_perks(user, segment_code)

        segment_id = user.segment_id or 1  # default to RETAIL
        items = await self._load_items(variant_quantities, segment_id)

        subtotal = sum((i.line_total for i in items), Decimal("0.00"))
        # The van is free from a limit the admin sets, and below it the option is
        # not offered: a firm that asks for it anyway is told what is missing.
        istanbul_min = ISTANBUL_FREE_MIN_DEFAULT
        if shipping_method is ShippingMethod.ISTANBUL_FREE:
            istanbul_min = await istanbul_free_minimum(self._db)
            if subtotal < istanbul_min:
                raise ValueError(
                    f"İstanbul içi ücretsiz teslimat {try_amount(istanbul_min)} TL ve üzeri "
                    f"siparişlerde geçerlidir; sepetinizde {try_amount(subtotal)} TL var"
                )
        shipping = self.quote_shipping(
            subtotal, items, shipping_method, perks=perks, istanbul_min=istanbul_min
        )
        # The three retail promotions, all of them off for a B2B account.
        volume_rate, volume_amount = (
            self.calculate_volume_discount(subtotal) if perks else (Decimal("0.00"), Decimal("0.00"))
        )
        # Sequential: the transfer discount applies to what's left after the volume discount.
        discount_rate, discount_amount = (
            self.calculate_discounts(subtotal, payment_method, base=subtotal - volume_amount)
            if perks
            else (Decimal("0.00"), Decimal("0.00"))
        )
        loyalty_points = self.calculate_loyalty_earnings(subtotal) if perks else 0

        # Points redemption: validate and cap
        points_discount = Decimal("0.00")
        actual_redeemed = 0
        if points_to_redeem > 0:
            if not perks:
                raise ValueError(PERKS_REFUSED)
            balance = await get_user_loyalty_balance(self._db, user.id)
            if points_to_redeem > balance:
                raise ValueError(
                    f"Insufficient ParaPuan: requested {points_to_redeem}, "
                    f"available {balance}"
                )
            # 1 point = 1.00 TRY, but cannot exceed the discounted subtotal
            max_redeemable = subtotal - volume_amount - discount_amount
            points_as_try = Decimal(points_to_redeem) * POINTS_TO_TRY
            points_discount = min(points_as_try, max_redeemable).quantize(Decimal("0.01"))
            actual_redeemed = int(points_discount / POINTS_TO_TRY)

        net_total = (
            subtotal + shipping.cost - volume_amount - discount_amount - points_discount
        ).quantize(Decimal("0.01"))

        return CartResult(
            items=items,
            subtotal=subtotal,
            cargo_cost=shipping.cost,
            shipping=shipping,
            volume_discount_rate=volume_rate,
            volume_discount_amount=volume_amount,
            discount_rate=discount_rate,
            discount_amount=discount_amount,
            points_discount=points_discount,
            net_total=net_total,
            loyalty_points_to_earn=loyalty_points,
            points_redeemed=actual_redeemed,
            payment_method=payment_method,
            perks_eligible=perks,
        )

    async def execute_checkout(
        self,
        user: User,
        variant_quantities: dict[uuid.UUID, int],
        payment_method: PaymentMethod,
        order_number: str,
        points_to_redeem: int = 0,
        shipping_method: ShippingMethod = ShippingMethod.YURTICI_DOOR,
        shipping_address: dict | None = None,
        shipping_consent: bool = False,
        receiver_name: str | None = None,
        receiver_phone: str | None = None,
        shipping_contract_no: str | None = None,
        freight_company_name: str | None = None,
        freight_phone: str | None = None,
        shipping_branch_name: str | None = None,
        invoice_to_company: bool = False,
        invoice_company_title: str | None = None,
        invoice_trade_name: str | None = None,
        invoice_tax_office: str | None = None,
        invoice_tax_number: str | None = None,
        invoice_address: dict | None = None,
        save_card: bool = False,
        distance_sales_accepted: bool = False,
        terms_accepted: bool = False,
    ) -> Order:
        """Execute the full checkout: calculate, create order, deduct inventory.

        Bank-transfer orders start as AWAITING_TRANSFER ("Havale Bekliyor") until an
        admin confirms the money; others start as PENDING_APPROVAL.

        `shipping_consent` is the buyer ticking "gönderim ücreti teslimatta alıcı
        tarafından ödenecektir"; a shipment the shop does not cover cannot be
        ordered without it.

        `receiver_name` / `receiver_phone` are "başka biri teslim alacak": who
        signs for the parcel when it is not the customer.

        `shipping_contract_no` is the buyer's own Yurtiçi agreement; the freight
        pair is the nakliye ambarı they want the goods handed to. Both optional,
        and both only on the method they belong to.

        `shipping_branch_name` is "Şubeme istiyorum": the firm's own branch this
        order is for, which is why its address is not the registered one.

        The invoice fields are "faturayı firmaya istiyorum": who the invoice is
        made out to and where that firm is registered, kept apart from the
        delivery address even when they name the same place.

        The two agreements — mesafeli satış sözleşmesi and şartlar ve kurallar —
        are not optional: an order is a contract, and this is where the shop
        records that both were accepted. A price preview never comes through
        here, so nothing is asked of a customer who is only looking.
        """
        if not distance_sales_accepted:
            raise ValueError("Mesafeli Satış Sözleşmesi'ni onaylamanız gerekiyor")
        if not terms_accepted:
            raise ValueError("Şartlar ve Kurallar Sözleşmesi'ni onaylamanız gerekiyor")

        cart = await self.resolve_cart(
            user, variant_quantities, payment_method, points_to_redeem, shipping_method
        )
        consent_at: datetime | None = None
        if cart.shipping.receiver_pays:
            if not shipping_consent:
                raise ValueError(
                    "Gönderim ücretinin teslimat sırasında alıcı tarafından ödeneceğini onaylamanız gerekiyor"
                )
            consent_at = datetime.now(timezone.utc)

        # Determine warehouse type from user segment
        warehouse = self._warehouse_for_segment(user)

        # Validate inventory and deduct
        await self._deduct_inventory(cart.items, warehouse)
        self._spend_offer_stock(cart.items)

        # Build order: discount_amount is volume + bank transfer + points
        total_discount = cart.volume_discount_amount + cart.discount_amount + cart.points_discount
        order_items = [
            OrderItem(
                variant_id=item.variant.id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in cart.items
        ]

        order = Order(
            order_number=order_number,
            user_id=user.id,
            total_amount=cart.subtotal,
            shipping_cost=cart.cargo_cost,
            discount_amount=total_discount,
            payment_method=payment_method,
            status=(
                OrderStatus.AWAITING_TRANSFER
                if payment_method == PaymentMethod.BANK_TRANSFER
                else OrderStatus.PENDING_APPROVAL
            ),
            shipping_method=shipping_method,
            shipping_payer=cart.shipping.payer,
            shipping_consent_at=consent_at,
            shipping_address=shipping_address,
            receiver_name=receiver_name,
            receiver_phone=receiver_phone,
            shipping_contract_no=shipping_contract_no,
            freight_company_name=freight_company_name,
            freight_phone=freight_phone,
            shipping_branch_name=shipping_branch_name,
            invoice_to_company=invoice_to_company,
            invoice_company_title=invoice_company_title,
            invoice_trade_name=invoice_trade_name,
            invoice_tax_office=invoice_tax_office,
            invoice_tax_number=invoice_tax_number,
            invoice_address=invoice_address,
            save_card=save_card,
            agreements_accepted_at=datetime.now(timezone.utc),
            items=order_items,
        )
        self._db.add(order)
        await self._db.flush()

        # Write SPENT ledger entry if points were redeemed
        if cart.points_redeemed > 0:
            self._db.add(LoyaltyLedger(
                user_id=user.id,
                order_id=order.id,
                amount=cart.points_redeemed,
                transaction_type=TransactionType.SPENT,
                source=PointSource.CHECKOUT,
            ))

        # NOTE: EARNED ledger entries are NOT written here. ParaPuan is
        # awarded only when an admin approves the order (see admin_orders.py).

        await self._db.commit()
        await self._db.refresh(order, attribute_names=["items"])
        return order

    # ── Discount calculation ─────────────────────────────────────────────

    @staticmethod
    def calculate_volume_discount(subtotal: Decimal) -> tuple[Decimal, Decimal]:
        """Return (rate, amount) of the volume discount ("ciro indirimi").

          - 30,000.00 – 49,999.99 TRY → 5%
          - >= 50,000.00 TRY          → 8%
        """
        if subtotal >= VOLUME_HIGH_THRESHOLD:
            rate = VOLUME_HIGH_RATE
        elif subtotal >= VOLUME_LOW_THRESHOLD:
            rate = VOLUME_LOW_RATE
        else:
            return Decimal("0.00"), Decimal("0.00")
        return rate, (subtotal * rate).quantize(Decimal("0.01"))

    @staticmethod
    def calculate_discounts(
        subtotal: Decimal,
        payment_method: PaymentMethod,
        base: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        """Return (rate, amount) of the bank transfer discount.

        The tier is judged on the subtotal; the rate applies to `base` (the
        amount left after the volume discount), or to the subtotal if omitted.
          - 1.00 – 29,999.99 TRY  → 5%
          - >= 30,000.00 TRY      → 7%
        Credit card: 0%
        """
        if payment_method != PaymentMethod.BANK_TRANSFER:
            return Decimal("0.00"), Decimal("0.00")

        if subtotal < Decimal("1.00"):
            return Decimal("0.00"), Decimal("0.00")

        if subtotal >= BANK_TRANSFER_HIGH_THRESHOLD:
            rate = BANK_TRANSFER_HIGH_RATE
        else:
            rate = BANK_TRANSFER_LOW_RATE

        amount = ((subtotal if base is None else base) * rate).quantize(Decimal("0.01"))
        return rate, amount

    # ── Cargo calculation ────────────────────────────────────────────────

    @staticmethod
    def quote_shipping(
        subtotal: Decimal,
        items: list[ResolvedCartItem],
        method: ShippingMethod = ShippingMethod.YURTICI_DOOR,
        *,
        perks: bool = True,
        istanbul_min: Decimal = ISTANBUL_FREE_MIN_DEFAULT,
    ) -> ShippingQuote:
        """Who pays the carriage, and why. The cost is always ₺0.

        The shop's own İstanbul run is free from `istanbul_min` upwards — the
        limit the panel sets. `resolve_cart` refuses the method below that, so
        the quote only says so for its own sake. Otherwise, for a
        retail cart the shop pays from 3,000 TRY, whether the parcel goes to a
        door or to a branch. Exception: in a coal-dominant cart (coal > 50%
        of the subtotal) less than 20 kg of coal goes "karşı ödemeli" (unknown
        weights count as 0 kg). At ≤ 50% coal the weight doesn't matter.

        Free shipping is one of the retail promotions, so ``perks=False`` — a
        B2B account — never reaches that threshold: a tier price does not
        include carriage, and Yurtiçi Kargo is then the same deal as a nakliye
        ambarı.

        A nakliye ambarı and an export shipment are never covered either: the
        depot collects its own fee, and freight and customs abroad are quoted
        after the order. Anything the shop does not cover is paid on delivery.
        """
        coal = [i for i in items if i.is_coal]
        coal_total = sum((i.line_total for i in coal), Decimal("0.00"))
        coal_ratio = (coal_total / subtotal).quantize(Decimal("0.0001")) if subtotal > 0 else Decimal("0")
        coal_kg = sum(
            (i.unit_weight_kg * i.quantity for i in coal if i.unit_weight_kg is not None), Decimal("0")
        )

        reason: ShippingReason
        if method == ShippingMethod.ISTANBUL_FREE:
            # The shop's own van: free from the limit the panel sets.
            reason = "free" if subtotal >= istanbul_min else "below_threshold"
        elif method == ShippingMethod.NAKLIYE_AMBARI:
            reason = "freight"  # the depot collects its fee from the receiver
        elif method == ShippingMethod.INTERNATIONAL_FREIGHT:
            reason = "international"  # quoted after the order: freight plus customs
        elif not perks:
            # A tier price does not include carriage, at any cart value.
            reason = "tier_price"
        elif subtotal < FREE_SHIPPING_THRESHOLD:
            reason = "below_threshold"
        elif coal_total / subtotal > COAL_MAX_RATIO and coal_kg < COAL_MIN_FREE_KG:
            reason = "coal_weight"
        else:
            reason = "free"

        return ShippingQuote(
            # The buyer is never charged carriage here; see the class docstring.
            cost=Decimal("0.00"),
            reason=reason,
            coal_total=coal_total,
            coal_ratio=coal_ratio,
            coal_weight_kg=coal_kg.quantize(Decimal("0.001")),
            coal_weight_unknown=any(i.unit_weight_kg is None for i in coal),
        )

    @staticmethod
    def calculate_cargo(subtotal: Decimal, items: list[ResolvedCartItem]) -> Decimal:
        return CheckoutEngine.quote_shipping(subtotal, items).cost

    # ── Loyalty / ParaPuan calculation ───────────────────────────────────

    @staticmethod
    def calculate_loyalty_earnings(subtotal: Decimal) -> int:
        return parapuan_for(subtotal)

    # ── Private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _spend_offer_stock(items: list[ResolvedCartItem]) -> None:
        """Draw down the "Fırsat stoğu" of every line bought at its offer price.

        At zero the offer stops being claimable (see `live_flash_sales`) while the
        product stays on the Fırsat list as "Fırsat Kaçtı". A product with no quota
        (null) runs until its deadline or the admin. Cancelling an order does not
        give the quota back: an order does not record which lines were on offer,
        so topping it up is the admin's call.
        """
        for item in items:
            quota = item.product.opportunity_stock
            if item.offer_applied and quota is not None:
                item.product.opportunity_stock = max(0, quota - item.quantity)

    @staticmethod
    async def _rates_for(variants: Iterable[ProductVariant], segment_id: int) -> Rates | None:
        """TCMB rates, fetched only when a price in this cart is in a foreign currency."""
        foreign = any(
            CurrencyType(p.currency) != CurrencyType.TRY
            for v in variants
            for p in v.prices
            if p.segment_id == segment_id
        )
        if not foreign:
            return None
        try:
            return await get_rates()
        except RatesUnavailable:
            return None

    async def _load_items(
        self,
        variant_quantities: dict[uuid.UUID, int],
        segment_id: int,
    ) -> list[ResolvedCartItem]:
        """Load variants with their products and segment prices."""
        variant_ids = list(variant_quantities.keys())

        result = await self._db.execute(
            select(ProductVariant)
            .where(ProductVariant.id.in_(variant_ids))
            .options(
                selectinload(ProductVariant.product),
                selectinload(ProductVariant.prices),
            )
        )
        variants = {v.id: v for v in result.scalars().all()}
        sales = await live_flash_sales(self._db, variant_ids, datetime.now(timezone.utc))
        coal_categories = set((await self._db.execute(category_subtree(COAL_CATEGORY_SLUG))).scalars())
        rates = await self._rates_for(variants.values(), segment_id)

        items: list[ResolvedCartItem] = []
        for vid, qty in variant_quantities.items():
            variant = variants.get(vid)
            if variant is None:
                raise ValueError(f"Variant {vid} not found")
            # A retired colour keeps its order lines but cannot be bought again.
            if not variant.is_active:
                raise ValueError(f"{variant.sku} artık satışta değil")

            price_row = pick_price_row(variant.prices, segment_id)
            if price_row is None:
                raise ValueError(f"{variant.sku} bu müşteri grubuna satılmıyor")
            # The cart is always TRY: a price entered in USD/EUR is converted at
            # the day's TCMB rate and locked in here.
            unit_price = to_try(price_row.price_incl_tax, price_row.currency, rates)
            if unit_price is None:
                raise ValueError(f"{variant.sku} için güncel döviz kuru alınamadı, lütfen sonra deneyin")

            sale_price = discounted_price(unit_price, sales.get(vid))
            items.append(
                ResolvedCartItem(
                    variant=variant,
                    product=variant.product,
                    unit_price=sale_price if sale_price is not None else unit_price,
                    quantity=qty,
                    offer_applied=sale_price is not None,
                    is_coal=variant.product.category_id in coal_categories,
                    unit_weight_kg=variant.weight_kg,
                )
            )

        return items

    @staticmethod
    def _warehouse_for_segment(user: User) -> WarehouseType:
        return warehouse_for_segment(user.segment.code if user.segment else None)

    async def _deduct_inventory(
        self,
        items: list[ResolvedCartItem],
        warehouse: WarehouseType,
    ) -> None:
        """Validate stock and deduct quantities atomically."""
        for item in items:
            result = await self._db.execute(
                select(Inventory).where(
                    Inventory.variant_id == item.variant.id,
                    Inventory.warehouse_type == warehouse,
                )
            )
            inv = result.scalar_one_or_none()

            if inv is None or inv.quantity < item.quantity:
                raise ValueError(
                    f"Insufficient stock for {item.variant.sku}: "
                    f"requested {item.quantity}, "
                    f"available {inv.quantity if inv else 0} "
                    f"in {warehouse.value}"
                )

            inv.quantity -= item.quantity
