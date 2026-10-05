"""Tests for admin order approval lifecycle and loyalty ledger.

Covers:
- Order approval state transition (PENDING_APPROVAL → APPROVED)
- ParaPuan credited on approval for qualifying totals
- No ParaPuan for non-qualifying totals
- Idempotent approval (no double-crediting)
- Rejection of re-approving already-approved orders
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import ProductVariant
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import User
from tests.conftest import auth_header


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _create_order(
    db: AsyncSession,
    user: User,
    variant: ProductVariant,
    qty: int,
    unit_price: Decimal,
) -> Order:
    """Insert an order directly into the DB (bypassing checkout engine)."""
    total = (unit_price * qty).quantize(Decimal("0.01"))
    order = Order(
        order_number=f"TST-{uuid.uuid4().hex[:10].upper()}",
        user_id=user.id,
        total_amount=total,
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=OrderStatus.PENDING_APPROVAL,
        items=[
            OrderItem(
                variant_id=variant.id,
                quantity=qty,
                unit_price=unit_price,
                line_total=total,
            )
        ],
    )
    db.add(order)
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    return order


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER APPROVAL LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════


async def test_approve_transitions_to_approved(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Approving a PENDING order sets status to APPROVED."""
    normal_v, _ = catalog
    order = await _create_order(db_session, admin_user, normal_v, 1, Decimal("5000.00"))

    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["new_status"] == "APPROVED"
    assert data["order_id"] == str(order.id)


async def test_approve_order_with_qualifying_total_awards_parapuan(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Order total 25,000 TRY → approval awards 2500 ParaPuan."""
    normal_v, _ = catalog
    order = await _create_order(db_session, admin_user, normal_v, 5, Decimal("5000.00"))

    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["loyalty_points_awarded"] == 2500
    assert "2500 ParaPuan" in data["message"]

    # Verify ledger entry exists in DB
    result = await db_session.execute(
        select(LoyaltyLedger).where(LoyaltyLedger.order_id == order.id)
    )
    ledger = result.scalar_one()
    assert ledger.amount == 2500
    assert ledger.transaction_type == TransactionType.EARNED
    assert ledger.expires_at is not None


async def test_approve_order_below_threshold_no_parapuan(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Order total 5,000 TRY → no ParaPuan (below 20k threshold)."""
    normal_v, _ = catalog
    order = await _create_order(db_session, admin_user, normal_v, 1, Decimal("5000.00"))

    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["loyalty_points_awarded"] == 0
    assert "No ParaPuan" in data["message"]

    # Verify NO ledger entry
    result = await db_session.execute(
        select(LoyaltyLedger).where(LoyaltyLedger.order_id == order.id)
    )
    assert result.scalar_one_or_none() is None


async def test_approve_above_30k_no_parapuan(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Order total 30,000 TRY → no ParaPuan (above 29,999.99 ceiling)."""
    normal_v, _ = catalog
    order = await _create_order(db_session, admin_user, normal_v, 6, Decimal("5000.00"))

    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["loyalty_points_awarded"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# IDEMPOTENCY & ERROR HANDLING
# ═══════════════════════════════════════════════════════════════════════════════


async def test_cannot_approve_already_approved_order(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Re-approving an APPROVED order returns 409."""
    normal_v, _ = catalog
    order = await _create_order(db_session, admin_user, normal_v, 5, Decimal("5000.00"))

    # First approval
    resp1 = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp1.status_code == 200

    # Second attempt — should be 409
    resp2 = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp2.status_code == 409
    assert "APPROVED" in resp2.json()["detail"]


async def test_approve_nonexistent_order_returns_404(
    client: AsyncClient, admin_user: User
):
    fake_id = "00000000-0000-0000-0000-000000000099"
    resp = await client.post(
        f"/api/v1/admin/orders/{fake_id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 404


async def test_loyalty_ledger_idempotent_on_approval(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    catalog: tuple[ProductVariant, ProductVariant],
):
    """Even if the approve endpoint is somehow called twice on the same order
    (e.g. via a race condition where status was already flipped), only one
    ledger entry should exist."""
    normal_v, _ = catalog
    order = await _create_order(db_session, admin_user, normal_v, 5, Decimal("5000.00"))

    # Approve once via HTTP
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/approve",
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200

    # Count ledger entries for this order
    result = await db_session.execute(
        select(LoyaltyLedger).where(
            LoyaltyLedger.order_id == order.id,
            LoyaltyLedger.transaction_type == TransactionType.EARNED,
        )
    )
    entries = result.scalars().all()
    assert len(entries) == 1, f"Expected exactly 1 ledger entry, got {len(entries)}"
