"""Tests for the 48-hour SLA worker that cancels expired bank-transfer orders.

Covers:
- Expired BANK_TRANSFER order gets cancelled
- Inventory is restored to the correct warehouse (RETAIL_STOCK for retail user)
- Inventory is restored to B2B_STOCK for wholesale user
- Non-expired orders are untouched
- CREDIT_CARD orders are never cancelled by the SLA worker
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services.order_tasks import cancel_expired_pending_orders


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _create_user(
    db: AsyncSession, segment_id: int, role: UserRole = UserRole.CUSTOMER
) -> User:
    user = User(
        email=f"sla-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=role,
        segment_id=segment_id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_variant_with_inventory(
    db: AsyncSession,
    retail_qty: int,
    b2b_qty: int,
    segment_id: int,
) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    prod = Product(base_code=f"SLA-{suffix}", name="SLA Test Product", tags=[])
    db.add(prod)
    await db.flush()

    variant = ProductVariant(product_id=prod.id, sku=f"SLA-{suffix}")
    db.add(variant)
    await db.flush()

    db.add(VariantPrice(
        variant_id=variant.id, segment_id=segment_id, currency="TRY",
        price_excl_tax=Decimal("100.00"), price_incl_tax=Decimal("118.00"),
        vat_rate=Decimal("18.00"),
    ))
    db.add(Inventory(
        variant_id=variant.id,
        warehouse_type=WarehouseType.RETAIL_STOCK,
        quantity=retail_qty,
    ))
    db.add(Inventory(
        variant_id=variant.id,
        warehouse_type=WarehouseType.B2B_STOCK,
        quantity=b2b_qty,
    ))
    await db.commit()
    return variant


async def _create_order(
    db: AsyncSession,
    user: User,
    variant: ProductVariant,
    qty: int,
    payment: PaymentMethod,
    created_hours_ago: int,
) -> Order:
    order = Order(
        order_number=f"SLA-{uuid.uuid4().hex[:10].upper()}",
        user_id=user.id,
        total_amount=Decimal("118.00") * qty,
        payment_method=payment,
        status=OrderStatus.PENDING_APPROVAL,
        items=[
            OrderItem(
                variant_id=variant.id,
                quantity=qty,
                unit_price=Decimal("118.00"),
                line_total=Decimal("118.00") * qty,
            )
        ],
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    # Backdate created_at via raw UPDATE to bypass the ORM default
    past = datetime.now(timezone.utc) - timedelta(hours=created_hours_ago)
    await db.execute(
        update(Order).where(Order.id == order.id).values(created_at=past)
    )
    await db.commit()
    await db.refresh(order)
    return order


async def _get_inventory(
    db: AsyncSession, variant_id: uuid.UUID, warehouse: WarehouseType
) -> int:
    result = await db.execute(
        select(Inventory).where(
            Inventory.variant_id == variant_id,
            Inventory.warehouse_type == warehouse,
        )
    )
    inv = result.scalar_one_or_none()
    return inv.quantity if inv else 0


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS
# ═══════════════════════════════════════════════════════════════════════════════


async def test_expired_bank_transfer_is_cancelled_and_retail_stock_restored(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """49-hour-old BANK_TRANSFER order → cancelled, RETAIL_STOCK restored."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _create_user(db_session, retail_seg.id)
    variant = await _create_variant_with_inventory(db_session, 100, 200, retail_seg.id)

    # Simulate checkout deduction
    inv = (await db_session.execute(
        select(Inventory).where(
            Inventory.variant_id == variant.id,
            Inventory.warehouse_type == WarehouseType.RETAIL_STOCK,
        )
    )).scalar_one()
    inv.quantity -= 3  # ordered 3 units
    await db_session.commit()

    assert await _get_inventory(db_session, variant.id, WarehouseType.RETAIL_STOCK) == 97

    order = await _create_order(
        db_session, user, variant, qty=3,
        payment=PaymentMethod.BANK_TRANSFER, created_hours_ago=49,
    )

    # Run SLA worker
    cancelled = await cancel_expired_pending_orders(db_session)
    assert cancelled == 1

    # Verify order status
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED

    # Verify RETAIL_STOCK restored: 97 + 3 = 100
    assert await _get_inventory(db_session, variant.id, WarehouseType.RETAIL_STOCK) == 100
    # B2B_STOCK untouched
    assert await _get_inventory(db_session, variant.id, WarehouseType.B2B_STOCK) == 200


async def test_expired_wholesale_order_restores_b2b_stock(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Wholesale user's expired order restores to B2B_STOCK, not RETAIL."""
    wholesale_seg = segments[SegmentType.WHOLESALE]
    user = await _create_user(db_session, wholesale_seg.id)
    # Use retail segment for pricing (doesn't matter for this test)
    retail_seg = segments[SegmentType.RETAIL]
    variant = await _create_variant_with_inventory(db_session, 100, 200, retail_seg.id)

    # Simulate B2B deduction
    inv = (await db_session.execute(
        select(Inventory).where(
            Inventory.variant_id == variant.id,
            Inventory.warehouse_type == WarehouseType.B2B_STOCK,
        )
    )).scalar_one()
    inv.quantity -= 10
    await db_session.commit()

    assert await _get_inventory(db_session, variant.id, WarehouseType.B2B_STOCK) == 190

    order = await _create_order(
        db_session, user, variant, qty=10,
        payment=PaymentMethod.BANK_TRANSFER, created_hours_ago=50,
    )

    cancelled = await cancel_expired_pending_orders(db_session)
    assert cancelled == 1

    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED

    # B2B restored: 190 + 10 = 200
    assert await _get_inventory(db_session, variant.id, WarehouseType.B2B_STOCK) == 200
    # RETAIL untouched
    assert await _get_inventory(db_session, variant.id, WarehouseType.RETAIL_STOCK) == 100


async def test_non_expired_order_is_not_cancelled(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Order created 24 hours ago is NOT cancelled."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _create_user(db_session, retail_seg.id)
    variant = await _create_variant_with_inventory(db_session, 100, 200, retail_seg.id)

    order = await _create_order(
        db_session, user, variant, qty=2,
        payment=PaymentMethod.BANK_TRANSFER, created_hours_ago=24,
    )

    cancelled = await cancel_expired_pending_orders(db_session)
    assert cancelled == 0

    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING_APPROVAL


async def test_credit_card_order_never_cancelled_by_sla(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """CREDIT_CARD orders are ignored by the SLA worker, even if old."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _create_user(db_session, retail_seg.id)
    variant = await _create_variant_with_inventory(db_session, 100, 200, retail_seg.id)

    order = await _create_order(
        db_session, user, variant, qty=5,
        payment=PaymentMethod.CREDIT_CARD, created_hours_ago=100,
    )

    cancelled = await cancel_expired_pending_orders(db_session)
    assert cancelled == 0

    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING_APPROVAL
