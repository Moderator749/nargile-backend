import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.order import Order
from app.models.user import User
from app.schemas.cart import CartCalculateResponse, CartItemBreakdown
from app.schemas.order import CheckoutRequest, OrderRead
from app.services.checkout_engine import CheckoutEngine

router = APIRouter(prefix="/orders", tags=["orders"])


def _generate_order_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short = uuid.uuid4().hex[:6].upper()
    return f"ORD-{ts}-{short}"


async def _get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.segment))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    return user


@router.post("/calculate-cart", response_model=CartCalculateResponse)
async def calculate_cart(
    payload: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
):
    """Preview mode — returns a full price breakdown without saving anything."""
    user = await _get_user(db, payload.user_id)
    variant_quantities = {item.variant_id: item.quantity for item in payload.items}

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
    db: AsyncSession = Depends(get_db),
):
    """Execute the full checkout: validate, calculate, persist order, deduct inventory."""
    user = await _get_user(db, payload.user_id)
    variant_quantities = {item.variant_id: item.quantity for item in payload.items}

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
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
