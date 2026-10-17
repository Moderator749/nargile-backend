import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.order import Order, PaymentMethod
from app.models.user import User
from app.api.v1.payments import CARD_OFF
from app.schemas.cart import CartCalculateResponse, CartItemBreakdown, ShippingBreakdown, VolumeTier
from app.schemas.order import CartItemInput, CheckoutRequest, OrderRead
from app.services.delivery import check_delivery_address
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
            shipping_method=payload.shipping_method,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    s = cart.shipping
    params = cart.params
    subtotal_excl = sum((i.line_total_excl_tax for i in cart.items), Decimal("0.00"))
    next_tier = params.next_volume_tier(cart.subtotal)
    return CartCalculateResponse(
        subtotal=cart.subtotal,
        perks_eligible=cart.perks_eligible,
        cargo_cost=cart.cargo_cost,
        shipping=ShippingBreakdown(
            free=s.free,
            receiver_pays=s.receiver_pays,
            reason=s.reason,
            free_threshold=params.free_shipping_threshold,
            coal_total=s.coal_total,
            coal_ratio=s.coal_ratio,
            coal_max_ratio=params.coal_max_ratio,
            coal_weight_kg=s.coal_weight_kg,
            coal_min_kg=params.coal_min_kg,
            coal_weight_unknown=s.coal_weight_unknown,
            coal_rule_basis=params.coal_rule_basis,
            coal_quantity=s.coal_quantity,
            coal_min_quantity=params.coal_min_quantity,
        ),
        volume_discount_rate=cart.volume_discount_rate,
        volume_discount_amount=cart.volume_discount_amount,
        volume_next_tier=(
            VolumeTier(threshold=next_tier.threshold, rate=next_tier.rate) if next_tier else None
        ),
        discount_rate=cart.discount_rate,
        discount_amount=cart.discount_amount,
        points_discount=cart.points_discount,
        net_total=cart.net_total,
        subtotal_excl_tax=subtotal_excl,
        vat_total=cart.subtotal - subtotal_excl,
        vat_rates=sorted({i.vat_rate for i in cart.items}),
        loyalty_points_to_earn=cart.loyalty_points_to_earn,
        payment_method=cart.payment_method,
        items=[
            CartItemBreakdown(
                variant_id=i.variant.id,
                product_id=i.product.id,
                product_slug=i.product.slug,
                sku=i.variant.sku,
                product_name=i.product.name,
                color_name=i.variant.color_name,
                quantity=i.quantity,
                unit_price=i.unit_price,
                line_total=i.line_total,
                tags=i.tags,
                image_url=i.product.image_url,
                vat_rate=i.vat_rate,
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
    if payload.shipping_address is None:
        raise HTTPException(status_code=422, detail="Teslimat adresi gerekli")
    # A card order is paid on PayTR's page; without the gateway it could only
    # sit holding stock, so it is not taken at all.
    if payload.payment_method == PaymentMethod.CREDIT_CARD and not settings.paytr_configured:
        raise HTTPException(status_code=422, detail=CARD_OFF)
    user = await _with_segment(db, current_user)
    variant_quantities = _quantities(payload.items)

    engine = CheckoutEngine(db)
    try:
        # A firm ships to the address it registered; the details are editable,
        # the place is not — unless it has named a branch of its own.
        await check_delivery_address(
            db, user, payload.shipping_address, payload.shipping_branch_name
        )
        order = await engine.execute_checkout(
            user=user,
            variant_quantities=variant_quantities,
            payment_method=payload.payment_method,
            order_number=_generate_order_number(),
            points_to_redeem=payload.points_to_redeem,
            shipping_method=payload.shipping_method,
            shipping_address=payload.shipping_address.model_dump(),
            shipping_consent=payload.shipping_consent,
            receiver_name=payload.receiver_name,
            receiver_phone=payload.receiver_phone,
            shipping_contract_no=payload.shipping_contract_no,
            freight_company_name=payload.freight_company_name,
            freight_phone=payload.freight_phone,
            shipping_branch_name=payload.shipping_branch_name,
            invoice_to_company=payload.invoice_to_company,
            invoice_company_title=payload.invoice_company_title,
            invoice_trade_name=payload.invoice_trade_name,
            invoice_tax_office=payload.invoice_tax_office,
            invoice_tax_number=payload.invoice_tax_number,
            invoice_address=(
                payload.invoice_address.model_dump() if payload.invoice_address else None
            ),
            save_card=payload.save_card,
            order_note=payload.order_note,
            transport_mode=payload.transport_mode,
            distance_sales_accepted=payload.distance_sales_accepted,
            terms_accepted=payload.terms_accepted,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return order


@router.get("", response_model=list[OrderRead])
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
