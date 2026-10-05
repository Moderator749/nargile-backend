"""Tests for the ParaPuan loyalty spending (redemption) engine.

Covers:
- Balance calculation (earned minus spent, expired excluded)
- Valid point redemption reduces net_total
- SPENT ledger entry written on checkout
- Overspend rejected (more than available balance)
- Expired points excluded from usable balance
- Points discount capped at discounted subtotal
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services.loyalty_service import get_user_loyalty_balance
from tests.conftest import auth_header


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _seed_user(db: AsyncSession, segment_id: int) -> User:
    user = User(
        email=f"loyal-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.CUSTOMER,
        segment_id=segment_id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _seed_variant(db: AsyncSession, segment_id: int, price: Decimal) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    prod = Product(base_code=f"LP-{suffix}", name="Loyalty Test Product", tags=[])
    db.add(prod)
    await db.flush()

    variant = ProductVariant(product_id=prod.id, sku=f"LP-{suffix}")
    db.add(variant)
    await db.flush()

    db.add(VariantPrice(
        variant_id=variant.id, segment_id=segment_id, currency="TRY",
        price_excl_tax=(price / Decimal("1.18")).quantize(Decimal("0.01")),
        price_incl_tax=price, vat_rate=Decimal("18.00"),
    ))
    db.add(Inventory(
        variant_id=variant.id,
        warehouse_type=WarehouseType.RETAIL_STOCK, quantity=999,
    ))
    await db.commit()
    return variant


async def _credit_points(
    db: AsyncSession, user_id: uuid.UUID, amount: int,
    expires_at: datetime | None = None,
) -> None:
    """Add EARNED points to a user's ledger."""
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=365)
    db.add(LoyaltyLedger(
        user_id=user_id,
        amount=amount,
        transaction_type=TransactionType.EARNED,
        expires_at=expires_at,
    ))
    await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# BALANCE CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════


async def test_balance_earned_minus_spent(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Balance = sum(EARNED) - sum(SPENT)."""
    user = await _seed_user(db_session, segments[SegmentType.RETAIL].id)

    await _credit_points(db_session, user.id, 1000)
    await _credit_points(db_session, user.id, 500)

    # Spend 300
    db_session.add(LoyaltyLedger(
        user_id=user.id, amount=300,
        transaction_type=TransactionType.SPENT,
    ))
    await db_session.commit()

    balance = await get_user_loyalty_balance(db_session, user.id)
    assert balance == 1200  # 1000 + 500 - 300


async def test_expired_points_excluded_from_balance(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Expired EARNED points don't count in available balance."""
    user = await _seed_user(db_session, segments[SegmentType.RETAIL].id)

    # 500 points still valid
    await _credit_points(db_session, user.id, 500)
    # 1000 points expired yesterday
    await _credit_points(
        db_session, user.id, 1000,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    balance = await get_user_loyalty_balance(db_session, user.id)
    assert balance == 500  # only the unexpired 500


async def test_zero_balance_for_new_user(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    user = await _seed_user(db_session, segments[SegmentType.RETAIL].id)
    balance = await get_user_loyalty_balance(db_session, user.id)
    assert balance == 0


# ═══════════════════════════════════════════════════════════════════════════════
# POINT REDEMPTION VIA HTTP
# ═══════════════════════════════════════════════════════════════════════════════


async def test_redeem_points_reduces_net_total(
    client: AsyncClient,
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Redeeming 500 points reduces net_total by 500 TRY."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _seed_user(db_session, retail_seg.id)
    variant = await _seed_variant(db_session, retail_seg.id, Decimal("5000.00"))

    await _credit_points(db_session, user.id, 500)

    # Calculate cart WITH 500 points
    resp = await client.post("/api/v1/orders/calculate-cart", json={
        "user_id": str(user.id),
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "points_to_redeem": 500,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(data["subtotal"]) == Decimal("5000.00")
    assert Decimal(data["points_discount"]) == Decimal("500.00")
    # net = 5000 + 0 cargo (above 3k) - 0 bank discount - 500 points = 4500
    assert Decimal(data["net_total"]) == Decimal("4500.00")


async def test_checkout_with_points_writes_spent_ledger(
    client: AsyncClient,
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Checkout with points creates a SPENT ledger entry."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _seed_user(db_session, retail_seg.id)
    variant = await _seed_variant(db_session, retail_seg.id, Decimal("5000.00"))

    await _credit_points(db_session, user.id, 300)

    resp = await client.post("/api/v1/orders/checkout", json={
        "user_id": str(user.id),
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "points_to_redeem": 300,
    })
    assert resp.status_code == 201
    order_id = resp.json()["id"]

    # Verify SPENT ledger entry
    result = await db_session.execute(
        select(LoyaltyLedger).where(
            LoyaltyLedger.order_id == uuid.UUID(order_id),
            LoyaltyLedger.transaction_type == TransactionType.SPENT,
        )
    )
    spent = result.scalar_one()
    assert spent.amount == 300
    assert spent.user_id == user.id

    # Balance should now be 0
    balance = await get_user_loyalty_balance(db_session, user.id)
    assert balance == 0


async def test_overspend_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Trying to redeem more points than available → 422."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _seed_user(db_session, retail_seg.id)
    variant = await _seed_variant(db_session, retail_seg.id, Decimal("5000.00"))

    await _credit_points(db_session, user.id, 100)

    resp = await client.post("/api/v1/orders/calculate-cart", json={
        "user_id": str(user.id),
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "points_to_redeem": 999,
    })
    assert resp.status_code == 422
    assert "Insufficient ParaPuan" in resp.json()["detail"]


async def test_points_discount_capped_at_subtotal(
    client: AsyncClient,
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """Points discount cannot exceed the discounted subtotal."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _seed_user(db_session, retail_seg.id)
    # Cheap product: 100 TRY
    variant = await _seed_variant(db_session, retail_seg.id, Decimal("100.00"))

    # Give user 5000 points but product only costs 100
    await _credit_points(db_session, user.id, 5000)

    resp = await client.post("/api/v1/orders/calculate-cart", json={
        "user_id": str(user.id),
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "points_to_redeem": 5000,
    })
    assert resp.status_code == 200
    data = resp.json()
    # Points discount capped at subtotal (100), not 5000
    assert Decimal(data["points_discount"]) == Decimal("100.00")
    # net = 100 + 49.90 cargo - 0 - 100 points = 49.90
    assert Decimal(data["net_total"]) == Decimal("49.90")


async def test_zero_points_works_normally(
    client: AsyncClient,
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """points_to_redeem=0 (default) — no points discount applied."""
    retail_seg = segments[SegmentType.RETAIL]
    user = await _seed_user(db_session, retail_seg.id)
    variant = await _seed_variant(db_session, retail_seg.id, Decimal("5000.00"))

    resp = await client.post("/api/v1/orders/calculate-cart", json={
        "user_id": str(user.id),
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(data["points_discount"]) == Decimal("0.00")
    assert Decimal(data["net_total"]) == Decimal("5000.00")
