"""Admin-only order management and approval endpoints."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.schemas.admin import OrderApproveResponse
from app.schemas.order import OrderRead
from app.services.checkout_engine import CheckoutEngine

router = APIRouter(prefix="/admin", tags=["admin-orders"])


@router.get("/orders", response_model=list[OrderRead])
async def list_orders(
    status_filter: OrderStatus | None = Query(None, alias="status"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    skip: int = 0,
    limit: int = 50,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Order).options(selectinload(Order.items))

    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    if date_from is not None:
        stmt = stmt.where(Order.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.created_at <= date_to)

    stmt = stmt.order_by(Order.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post(
    "/orders/{order_id}/approve",
    response_model=OrderApproveResponse,
)
async def approve_order(
    order_id: str,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve a PENDING_APPROVAL order.

    If the order total qualifies for ParaPuan, the EARNED ledger entry is
    written here — not at checkout — ensuring points are only granted for
    approved orders.
    """
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != OrderStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Order cannot be approved — current status is {order.status.value}",
        )

    # Transition status
    order.status = OrderStatus.APPROVED

    # Calculate loyalty eligibility from the order's subtotal (total_amount)
    loyalty_points = CheckoutEngine.calculate_loyalty_earnings(order.total_amount)

    if loyalty_points > 0:
        # Check idempotency — don't double-award if re-called
        existing = await db.execute(
            select(LoyaltyLedger).where(
                LoyaltyLedger.order_id == order.id,
                LoyaltyLedger.transaction_type == TransactionType.EARNED,
            )
        )
        if existing.scalar_one_or_none() is None:
            ledger_entry = LoyaltyLedger(
                user_id=order.user_id,
                order_id=order.id,
                amount=loyalty_points,
                transaction_type=TransactionType.EARNED,
                expires_at=datetime.now(timezone.utc) + timedelta(days=365),
            )
            db.add(ledger_entry)

    await db.commit()

    return OrderApproveResponse(
        order_id=order.id,
        order_number=order.order_number,
        new_status=OrderStatus.APPROVED.value,
        loyalty_points_awarded=loyalty_points,
        message=(
            f"Order approved. {loyalty_points} ParaPuan awarded."
            if loyalty_points > 0
            else "Order approved. No ParaPuan earned for this order total."
        ),
    )
