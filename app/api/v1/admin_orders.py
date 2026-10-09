"""Admin-only order management and approval endpoints."""

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.catalog import CurrencyType, Product, ProductVariant, VariantPrice
from app.models.order import PENDING_STATUSES, Order, OrderItem, OrderStatus, ShippingMethod
from app.models.user import AddressKind, User
from app.core.countries import TURKEY, country_name
from app.schemas.address import address_line, international_line
from app.schemas.admin import AdminOrderItemDetail, AdminOrderListItem, OrderApproveResponse
from app.schemas.order import OrderItemRead, OrderRead
from app.services import mailer, order_emails, order_lifecycle

router = APIRouter(prefix="/admin", tags=["admin-orders"])


@router.get("/orders", response_model=list[AdminOrderListItem])
async def list_orders(
    response: Response,
    status_filter: OrderStatus | None = Query(None, alias="status"),
    shipping_method: ShippingMethod | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Order, User)
        .join(User, Order.user_id == User.id)
        .options(selectinload(Order.items), selectinload(User.profile), selectinload(User.addresses))
    )
    filters = []

    if status_filter is not None:
        filters.append(Order.status == status_filter)
    if shipping_method is not None:
        filters.append(Order.shipping_method == shipping_method)
    if date_from is not None:
        filters.append(Order.created_at >= date_from)
    if date_to is not None:
        filters.append(Order.created_at <= date_to)
    stmt = stmt.where(*filters)
    # Total matches, for page controls.
    response.headers["X-Total-Count"] = str(await db.scalar(select(func.count()).select_from(Order).where(*filters)))

    stmt = stmt.order_by(Order.created_at.desc()).offset(skip).limit(limit)
    rows = (await db.execute(stmt)).all()
    catalog = await _line_catalog(db, [order for order, _ in rows])
    return [_list_item(order, user, catalog) for order, user in rows]


# Per ordered variant: (sku, product code, product name, colour, VAT rate).
LineCatalog = dict[uuid.UUID, tuple[str, str, str, str | None, Decimal | None]]


async def _line_catalog(db: AsyncSession, orders: Sequence[Order]) -> LineCatalog:
    """Catalogue data for the ordered variants, in one query, for the detail sheet.

    The order lines store only ids and prices, but the order sheet prints the
    product code, the name, the colour and the VAT the price includes.
    """
    variant_ids = {item.variant_id for order in orders for item in order.items}
    if not variant_ids:
        return {}
    stmt = (
        select(
            ProductVariant.id,
            ProductVariant.sku,
            Product.base_code,
            Product.name,
            ProductVariant.color_name,
            # The rate belongs to the product, so it is the same in every
            # segment's price row; max() just collapses them to one.
            func.max(VariantPrice.vat_rate),
        )
        .join(Product, ProductVariant.product_id == Product.id)
        .outerjoin(
            VariantPrice,
            (VariantPrice.variant_id == ProductVariant.id) & (VariantPrice.currency == CurrencyType.TRY),
        )
        .where(ProductVariant.id.in_(variant_ids))
        .group_by(
            ProductVariant.id,
            ProductVariant.sku,
            Product.base_code,
            Product.name,
            ProductVariant.color_name,
        )
    )
    return {row[0]: tuple(row[1:]) for row in (await db.execute(stmt)).all()}


def _line(item: OrderItem, catalog: LineCatalog) -> AdminOrderItemDetail:
    sku, product_code, product_name, color_name, vat_rate = catalog.get(
        item.variant_id, (None, None, None, None, None)
    )
    return AdminOrderItemDetail(
        **OrderItemRead.model_validate(item).model_dump(),
        sku=sku,
        product_code=product_code,
        product_name=product_name,
        color_name=color_name,
        vat_rate=vat_rate,
    )


def _billing_address(order: Order, user: User) -> dict | None:
    """Where this order's invoice goes, in the shape the order sheet prints —
    the same five fields as the delivery address it sits beside, plus the
    country when it is not this one.

    The order's own snapshot wins when it has one: a buyer who asked for the
    invoice to be made out to a firm gave that firm's registered address at
    checkout, and the invoice has to show the address as it was then, not as the
    account looks later.

    Otherwise it is the customer's standing invoice address, and None when they
    never gave one: a retail buyer is invoiced where they are delivered, and the
    sheet says so.
    """
    if order.invoice_address:
        snapshot: dict = order.invoice_address
        return {
            "full_name": order.invoice_company_title or user.full_name,
            "phone": user.phone,
            "city": snapshot.get("city"),
            "district": snapshot.get("district"),
            "line": snapshot.get("line"),
        }
    # The firm's address when the invoice is made out to one, the customer's own
    # otherwise: an invoice goes where it is addressed.
    by_kind = {a.kind: a for a in user.addresses}
    row = by_kind.get(AddressKind.CORPORATE_BILLING) or by_kind.get(AddressKind.BILLING)
    if row is None:
        return None
    profile = user.profile
    domestic = row.country == TURKEY
    return {
        "full_name": (profile.company_title if profile else None) or user.full_name,
        "phone": user.phone,
        "city": row.city,
        "district": row.district,
        "line": address_line(
            neighbourhood=row.neighbourhood,
            street=row.street,
            building_no=row.building_no,
            building_name=row.building_name,
            door_no=row.door_no,
        )
        if domestic
        else international_line(
            address_line1=row.address_line1,
            address_line2=row.address_line2,
            postal_code=row.postal_code,
        ),
        **({} if domestic else {"country": country_name(row.country)}),
    }


def _list_item(order: Order, user: User, catalog: LineCatalog) -> AdminOrderListItem:
    return AdminOrderListItem(
        **OrderRead.model_validate(order).model_dump(exclude={"items"}),
        customer_email=user.email,
        customer_name=user.full_name,
        customer_phone=user.phone,
        company_title=user.profile.company_title if user.profile else None,
        billing_address=_billing_address(order, user),
        items=[_line(item, catalog) for item in order.items],
    )


# Pending orders, plus a paid one the shop decides not to ship after all.
CANCELLABLE_STATUSES = (*PENDING_STATUSES, OrderStatus.APPROVED)


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ShipOrderRequest(BaseModel):
    """Carrier tracking code, if the shipment has one (courier runs do not)."""

    tracking_code: str | None = Field(default=None, max_length=100)


async def _order_with_user(db: AsyncSession, order_id: uuid.UUID) -> tuple[Order, User]:
    order = await db.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    user = await db.scalar(
        select(User)
        .where(User.id == order.user_id)
        .options(selectinload(User.segment), selectinload(User.profile), selectinload(User.addresses))
    )
    return order, user


@router.post(
    "/orders/{order_id}/approve",
    response_model=OrderApproveResponse,
)
async def approve_order(
    order_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve / mark paid a pending order (incl. "Havale Bekliyor" once the money
    has arrived). Points are granted here, never at checkout: ParaPuan for a
    20,000–29,999.99 order, Kumbarapuan once smaller paid orders reach 20,000."""
    order, _user = await _order_with_user(db, order_id)
    if order.status not in PENDING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Order cannot be approved — current status is {order.status.value}",
        )

    parapuan, kumbara = await order_lifecycle.mark_paid(db, order, datetime.now(timezone.utc))
    await db.commit()

    parts = [f"{parapuan} ParaPuan" if parapuan else "", f"{kumbara} Kumbarapuan" if kumbara else ""]
    awarded = " + ".join(p for p in parts if p)
    return OrderApproveResponse(
        order_id=order.id,
        order_number=order.order_number,
        new_status=OrderStatus.APPROVED.value,
        loyalty_points_awarded=parapuan + kumbara,
        message=f"Order approved. {awarded} awarded." if awarded else "Order approved. No points earned for this order.",
        paid_at=order.paid_at,
    )


@router.post("/orders/{order_id}/ship", response_model=AdminOrderListItem)
async def ship_order(
    order_id: uuid.UUID,
    payload: ShipOrderRequest,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Hand a paid order to the carrier and tell the customer it is on its way.

    The dispatch e-mail is best-effort: with no SMTP server configured the mailer
    logs the message, and a send failure never undoes the dispatch.
    """
    order, user = await _order_with_user(db, order_id)
    if order.status != OrderStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=f"Only approved orders can be shipped — current status is {order.status.value}",
        )
    await order_lifecycle.mark_shipped(db, order, payload.tracking_code)
    await db.commit()
    await db.refresh(order, attribute_names=["items"])

    catalog = await _line_catalog(db, [order])
    lines = [
        (catalog.get(item.variant_id, (None, None, None, None))[2] or "Ürün", item.quantity)
        for item in order.items
    ]
    await mailer.send(order_emails.order_shipped(order, user.full_name, user.email, lines))
    return _list_item(order, user, catalog)


@router.post("/orders/{order_id}/cancel", response_model=AdminOrderListItem)
async def cancel_order(
    order_id: uuid.UUID,
    payload: CancelOrderRequest,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Reject / cancel an order that has not shipped: stock goes back, spent
    points return and points earned on it (if it was already paid) are void."""
    order, user = await _order_with_user(db, order_id)
    if order.status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Only orders that have not shipped can be cancelled — current status is {order.status.value}",
        )
    await order_lifecycle.cancel(db, order, user, payload.reason.strip(), datetime.now(timezone.utc))
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    # Same shape as the list, so the admin's open order sheet can replace the row.
    return _list_item(order, user, await _line_catalog(db, [order]))
