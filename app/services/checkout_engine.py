"""Checkout engine implementing Turhan Abi's commercial rules.

Business rules (all tiers are judged on the merchandise subtotal):
- Volume discount ("ciro indirimi"): 5% for 30,000-49,999.99 TRY, 8% for >= 50,000
- Bank transfer discount: 5% for 1-29,999.99 TRY, 7% for >= 30,000, applied
  sequentially: on the amount left after the volume discount
- ParaPuan/Kumbarapuan: granted when an order is paid (see app.services.wallet)
- ParaPuan redemption: 1 point = 1.00 TRY, capped at the discounted subtotal
- Cargo: the shop never charges carriage on the order — the cost is always ₺0.
  Free from 3,000 TRY means the shop pays the carrier; below that the shipment
  goes "karşı ödemeli" and the buyer pays the carrier on delivery (which the
  buyer confirms with a checkbox at checkout). The 20 kg coal minimum applies
  only to coal-dominant carts (coal, i.e. the Kömürler category, worth more than
  50% of the subtotal): those ship on the shop only from 20 kg of coal (unknown
  weights count as 0 kg). Where hardware dominates, its margin absorbs the freight.
- Istanbul courier (same day, 10:00–20:00): the buyer pays the courier on
  delivery, by distance
- Flash sales: a live sale price replaces the segment price when lower; once
  the sale ends the segment price applies again
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import (
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod, ShippingMethod, ShippingPayer
from app.models.user import SegmentType, User
from app.services.categories import COAL_CATEGORY_SLUG, category_subtree
from app.services.flash_sales import discounted_price, live_flash_sales
from app.services.loyalty_service import get_user_loyalty_balance
from app.services.wallet import parapuan_for
from app.services.pricing_engine import calculate_line_total

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


def warehouse_for_segment(code: SegmentType | None) -> WarehouseType:
    """Map a segment to the warehouse it buys from.

    WHOLESALE / DEALER → B2B_STOCK
    RETAIL / CAFE (or no segment) → RETAIL_STOCK
    """
    if code in (SegmentType.WHOLESALE, SegmentType.DEALER):
        return WarehouseType.B2B_STOCK
    return WarehouseType.RETAIL_STOCK


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
    # kg per unit; None = not recorded.
    unit_weight_kg: Decimal | None = None

    def __post_init__(self) -> None:
        self.line_total = calculate_line_total(self.unit_price, self.quantity)
        self.tags = self.product.tags or []

# ── Shipping quote ───────────────────────────────────────────────────────────

ShippingReason = Literal["free", "below_threshold", "coal_weight", "courier"]


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
        shipping_method: ShippingMethod = ShippingMethod.CARGO,
    ) -> CartResult:
        """Resolve raw cart items into a full calculation with all business rules."""
        segment_id = user.segment_id or 1  # default to RETAIL
        items = await self._load_items(variant_quantities, segment_id)

        subtotal = sum((i.line_total for i in items), Decimal("0.00"))
        shipping = self.quote_shipping(subtotal, items, shipping_method)
        volume_rate, volume_amount = self.calculate_volume_discount(subtotal)
        # Sequential: the transfer discount applies to what's left after the volume discount.
        discount_rate, discount_amount = self.calculate_discounts(
            subtotal, payment_method, base=subtotal - volume_amount
        )
        loyalty_points = self.calculate_loyalty_earnings(subtotal)

        # Points redemption: validate and cap
        points_discount = Decimal("0.00")
        actual_redeemed = 0
        if points_to_redeem > 0:
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
        )

    async def execute_checkout(
        self,
        user: User,
        variant_quantities: dict[uuid.UUID, int],
        payment_method: PaymentMethod,
        order_number: str,
        points_to_redeem: int = 0,
        shipping_method: ShippingMethod = ShippingMethod.CARGO,
        shipping_address: dict | None = None,
        shipping_consent: bool = False,
    ) -> Order:
        """Execute the full checkout: calculate, create order, deduct inventory.

        Bank-transfer orders start as AWAITING_TRANSFER ("Havale Bekliyor") until an
        admin confirms the money; others start as PENDING_APPROVAL.

        `shipping_consent` is the buyer ticking "gönderim ücreti teslimatta alıcı
        tarafından ödenecektir"; a shipment the shop does not cover cannot be
        ordered without it.
        """
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
        subtotal: Decimal, items: list[ResolvedCartItem], method: ShippingMethod = ShippingMethod.CARGO
    ) -> ShippingQuote:
        """Who pays the carriage, and why. The cost is always ₺0.

        The shop pays from 3,000 TRY. Exception: in a coal-dominant cart (coal >
        50% of the subtotal) less than 20 kg of coal goes "karşı ödemeli"
        (unknown weights count as 0 kg). At ≤ 50% coal the weight doesn't matter.
        Anything the shop does not cover is paid to the carrier on delivery.
        """
        coal = [i for i in items if i.is_coal]
        coal_total = sum((i.line_total for i in coal), Decimal("0.00"))
        coal_ratio = (coal_total / subtotal).quantize(Decimal("0.0001")) if subtotal > 0 else Decimal("0")
        coal_kg = sum(
            (i.unit_weight_kg * i.quantity for i in coal if i.unit_weight_kg is not None), Decimal("0")
        )

        reason: ShippingReason
        if method == ShippingMethod.COURIER_ISTANBUL:
            reason = "courier"  # the courier's fee is paid to the courier on delivery
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

        items: list[ResolvedCartItem] = []
        for vid, qty in variant_quantities.items():
            variant = variants.get(vid)
            if variant is None:
                raise ValueError(f"Variant {vid} not found")

            # Find the segment-specific TRY price
            price_row = next(
                (
                    p
                    for p in variant.prices
                    if p.segment_id == segment_id and p.currency == "TRY"
                ),
                None,
            )
            if price_row is None:
                raise ValueError(
                    f"No TRY price for variant {variant.sku} in segment {segment_id}"
                )

            sale_price = discounted_price(price_row.price_incl_tax, sales.get(vid))
            items.append(
                ResolvedCartItem(
                    variant=variant,
                    product=variant.product,
                    unit_price=sale_price if sale_price is not None else price_row.price_incl_tax,
                    quantity=qty,
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
