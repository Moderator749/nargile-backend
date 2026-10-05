"""Checkout engine implementing Turhan Abi's commercial rules.

Business rules:
- Bank transfer discount: 5% for 1-29999.99 TRY, 7% for >=30000 TRY
- ParaPuan loyalty: 10% earned when cart is 20000-29999.99 TRY
- ParaPuan redemption: 1 point = 1.00 TRY, capped at discounted subtotal
- Anti-fraud cargo: free shipping at 3000 TRY, but HEAVY_CARGO items
  cannot exceed 50% of cart value to qualify
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

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
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import SegmentType, User
from app.services.loyalty_service import get_user_loyalty_balance
from app.services.pricing_engine import calculate_line_total

# ── Constants ────────────────────────────────────────────────────────────────

FREE_SHIPPING_THRESHOLD = Decimal("3000.00")
HEAVY_CARGO_MAX_RATIO = Decimal("0.50")
FLAT_SHIPPING_RATE = Decimal("49.90")
HEAVY_CARGO_SURCHARGE = Decimal("89.90")

BANK_TRANSFER_LOW_CEILING = Decimal("29999.99")
BANK_TRANSFER_LOW_RATE = Decimal("0.05")
BANK_TRANSFER_HIGH_THRESHOLD = Decimal("30000.00")
BANK_TRANSFER_HIGH_RATE = Decimal("0.07")

LOYALTY_MIN_THRESHOLD = Decimal("20000.00")
LOYALTY_MAX_THRESHOLD = Decimal("29999.99")
LOYALTY_EARN_RATE = Decimal("0.10")

POINTS_TO_TRY = Decimal("1.00")  # 1 ParaPuan = 1.00 TRY


# ── Resolved cart item (after DB lookup) ─────────────────────────────────────

@dataclass
class ResolvedCartItem:
    variant: ProductVariant
    product: Product
    unit_price: Decimal
    quantity: int
    line_total: Decimal = field(init=False)
    tags: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.line_total = calculate_line_total(self.unit_price, self.quantity)
        self.tags = self.product.tags or []


# ── Cart calculation result ──────────────────────────────────────────────────

@dataclass
class CartResult:
    items: list[ResolvedCartItem]
    subtotal: Decimal
    cargo_cost: Decimal
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
    ) -> CartResult:
        """Resolve raw cart items into a full calculation with all business rules."""
        segment_id = user.segment_id or 1  # default to RETAIL
        items = await self._load_items(variant_quantities, segment_id)

        subtotal = sum(i.line_total for i in items)
        cargo_cost = self.calculate_cargo(subtotal, items)
        discount_rate, discount_amount = self.calculate_discounts(subtotal, payment_method)
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
            # 1 point = 1.00 TRY, but cannot exceed (subtotal - bank discount)
            max_redeemable = subtotal - discount_amount
            points_as_try = Decimal(points_to_redeem) * POINTS_TO_TRY
            points_discount = min(points_as_try, max_redeemable).quantize(Decimal("0.01"))
            actual_redeemed = int(points_discount / POINTS_TO_TRY)

        net_total = (
            subtotal + cargo_cost - discount_amount - points_discount
        ).quantize(Decimal("0.01"))

        return CartResult(
            items=items,
            subtotal=subtotal,
            cargo_cost=cargo_cost,
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
    ) -> Order:
        """Execute the full checkout: calculate, create order, deduct inventory."""
        cart = await self.resolve_cart(
            user, variant_quantities, payment_method, points_to_redeem
        )

        # Determine warehouse type from user segment
        warehouse = self._warehouse_for_segment(user)

        # Validate inventory and deduct
        await self._deduct_inventory(cart.items, warehouse)

        # Build order — discount_amount includes both bank transfer + points
        total_discount = cart.discount_amount + cart.points_discount
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
            status=OrderStatus.PENDING_APPROVAL,
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
            ))

        # NOTE: EARNED ledger entries are NOT written here. ParaPuan is
        # awarded only when an admin approves the order (see admin_orders.py).

        await self._db.commit()
        await self._db.refresh(order, attribute_names=["items"])
        return order

    # ── Discount calculation ─────────────────────────────────────────────

    @staticmethod
    def calculate_discounts(
        subtotal: Decimal,
        payment_method: PaymentMethod,
    ) -> tuple[Decimal, Decimal]:
        """Return (discount_rate, discount_amount) for the given subtotal.

        Bank transfer discount:
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

        amount = (subtotal * rate).quantize(Decimal("0.01"))
        return rate, amount

    # ── Cargo calculation (anti-fraud) ───────────────────────────────────

    @staticmethod
    def calculate_cargo(
        subtotal: Decimal,
        items: list[ResolvedCartItem],
    ) -> Decimal:
        """Compute shipping with anti-fraud heavy cargo check.

        Free shipping at 3,000 TRY — UNLESS HEAVY_CARGO items represent
        more than 50% of the cart's total value, in which case shipping is
        always charged.
        """
        heavy_total = sum(
            i.line_total for i in items if "HEAVY_CARGO" in i.tags
        )
        has_heavy = heavy_total > Decimal("0")

        # Anti-fraud: if heavy cargo > 50% of cart value, never grant free shipping
        heavy_exceeds_limit = (
            has_heavy and subtotal > 0 and heavy_total / subtotal > HEAVY_CARGO_MAX_RATIO
        )

        if heavy_exceeds_limit:
            return HEAVY_CARGO_SURCHARGE

        if subtotal >= FREE_SHIPPING_THRESHOLD:
            return HEAVY_CARGO_SURCHARGE if has_heavy else Decimal("0.00")

        return HEAVY_CARGO_SURCHARGE if has_heavy else FLAT_SHIPPING_RATE

    # ── Loyalty / ParaPuan calculation ───────────────────────────────────

    @staticmethod
    def calculate_loyalty_earnings(subtotal: Decimal) -> int:
        """Calculate ParaPuan earned.

        Strictly between 20,000.00 and 29,999.99 TRY → 10% as integer points.
        """
        if LOYALTY_MIN_THRESHOLD <= subtotal <= LOYALTY_MAX_THRESHOLD:
            return int((subtotal * LOYALTY_EARN_RATE).quantize(Decimal("1")))
        return 0

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

            items.append(
                ResolvedCartItem(
                    variant=variant,
                    product=variant.product,
                    unit_price=price_row.price_incl_tax,
                    quantity=qty,
                )
            )

        return items

    @staticmethod
    def _warehouse_for_segment(user: User) -> WarehouseType:
        """Map user segment to the correct warehouse type.

        WHOLESALE / DEALER → B2B_STOCK
        RETAIL / CAFE (or no segment) → RETAIL_STOCK
        """
        if user.segment and user.segment.code in (
            SegmentType.WHOLESALE,
            SegmentType.DEALER,
        ):
            return WarehouseType.B2B_STOCK
        return WarehouseType.RETAIL_STOCK

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
