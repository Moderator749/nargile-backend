import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.order import Order
from app.models.user import User
from app.schemas.cart import CartCalculateResponse, CartItemBreakdown
from app.schemas.order import CartItemInput, CheckoutRequest, OrderRead
from app.services.checkout_engine import CheckoutEngine

router = APIRouter(prefix="/orders", tags=["orders"])


def _generate_order_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short = uuid.uuid4().hex[:6].upper()
    return f"ORD-{ts}-{short}"


async def _with_segment(db: AsyncSession, user: User) -> User:
    """Reload the authenticated user with its segment eager-loaded (the checkout
    engine reads user.segment, which cannot lazy-load under AsyncSession)."""
    result = await db.execute(
        select(User).where(User.id == user.id).options(selectinload(User.segment))
    )
    return result.scalar_one()


def _quantities(items: list[CartItemInput]) -> dict[uuid.UUID, int]:
    # Sum duplicate lines instead of letting the last one silently win.
    totals: dict[uuid.UUID, int] = {}
    for item in items:
        totals[item.variant_id] = totals.get(item.variant_id, 0) + item.quantity
    return totals


@router.post("/calculate-cart", response_model=CartCalculateResponse)
async def calculate_cart(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview mode — returns a full price breakdown without saving anything."""
    user = await _with_segment(db, current_user)
    variant_quantities = _quantities(payload.items)

    engine = CheckoutEngine(db)
    try:
        cart = await engine.resolve_cart(
            user, variant_quantities, payload.payment_method,
            points_to_redeem=payload.points_to_redeem,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return CartCalculateResponse(
        subtotal=cart.subtotal,
        cargo_cost=cart.cargo_cost,
        discount_rate=cart.discount_rate,
        discount_amount=cart.discount_amount,
        points_discount=cart.points_discount,
        net_total=cart.net_total,
        loyalty_points_to_earn=cart.loyalty_points_to_earn,
        payment_method=cart.payment_method,
        items=[
            CartItemBreakdown(
                variant_id=i.variant.id,
                sku=i.variant.sku,
                product_name=i.product.name,
                quantity=i.quantity,
                unit_price=i.unit_price,
                line_total=i.line_total,
                tags=i.tags,
            )
            for i in cart.items
        ],
    )


@router.post("/checkout", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
async def checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute the full checkout: validate, calculate, persist order, deduct inventory."""
    user = await _with_segment(db, current_user)
    variant_quantities = _quantities(payload.items)

    engine = CheckoutEngine(db)
    try:
        order = await engine.execute_checkout(
            user=user,
            variant_quantities=variant_quantities,
            payment_method=payload.payment_method,
            order_number=_generate_order_number(),
            points_to_redeem=payload.points_to_redeem,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return order


@router.get("/", response_model=list[OrderRead])
async def list_orders(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The authenticated user's own orders, newest first."""
    result = await db.execute(
        select(Order)
        .where(Order.user_id == current_user.id)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(
    order_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Another user's order is reported as 404, not 403, to avoid confirming it exists.
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id, Order.user_id == current_user.id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
