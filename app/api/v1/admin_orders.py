"""Admin-only order management and approval endpoints."""

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user, get_session_factory
from app.core.database import get_db
from app.models.catalog import CurrencyType, Product, ProductVariant, VariantPrice
from app.core.config import settings
from app.models.order import PENDING_STATUSES, Carrier, Order, OrderItem, OrderStatus, ShippingMethod
from app.models.user import AddressKind, User
from app.core.countries import TURKEY, country_name
from app.schemas.address import address_line, international_line
from app.schemas.admin import AdminOrderItemDetail, AdminOrderListItem, OrderApproveResponse
from app.schemas.order import OrderItemRead, OrderRead
from app.services import mailer, order_emails, order_lifecycle, order_sms, shipping, sms_settings

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


@router.get("/orders/{order_id}", response_model=AdminOrderListItem)
async def get_order(
    order_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """One order as the sheet prints it.

    The listing above is a page of orders, read when the page is read; this is
    the order as it stands right now. The sheet asks for it when it opens, so
    what an admin reads is never a row the panel happened to fetch earlier —
    before the customer's note was written, before an address was corrected.
    """
    order, user = await _order_with_user(db, order_id)
    return _list_item(order, user, await _line_catalog(db, [order]))


# Pending orders, plus a paid one the shop decides not to ship after all.
CANCELLABLE_STATUSES = (*PENDING_STATUSES, OrderStatus.APPROVED)


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ShipOrderRequest(BaseModel):
    """"Kargola": who takes the parcel, and how it is tracked.

    Everything is optional, so an empty body still ships the order: the carrier
    then follows from the delivery the customer chose (see
    ``shipping.default_carrier``) and there is no tracking code — the shop's own
    van has none. ``use_api`` registers the shipment with the carrier's web
    service instead of taking a code (Yurtiçi only); its tracking number
    arrives once the branch scans the label.
    """

    tracking_code: str | None = Field(default=None, max_length=100)
    carrier: Carrier | None = None
    # The carrier's or the nakliye ambarı's name, for OTHER.
    carrier_name: str | None = Field(default=None, max_length=100)
    use_api: bool = False


class ShippingIntegrationRead(BaseModel):
    """What "Kargola" can offer: a web service per carrier, and in which mode."""

    yurtici_api: bool
    # The alıcı ödemeli account too: karşı ödemeli orders go through the web service.
    yurtici_api_receiver: bool
    yurtici_test_mode: bool
    yurtici_customer_code: str | None


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
    has arrived). Sultan Puan is granted here, never at checkout, by the live
    loyalty parameters: the standard award for an order inside the band, and
    the annual bonus when this order brings the rolling spend to the target."""
    order, _user = await _order_with_user(db, order_id)
    if order.status not in PENDING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Order cannot be approved — current status is {order.status.value}",
        )

    standard, bonus = await order_lifecycle.mark_paid(db, order, datetime.now(timezone.utc))
    await db.commit()

    parts = [
        f"{standard} Sultan Puan" if standard else "",
        f"{bonus} Sultan Puan annual bonus" if bonus else "",
    ]
    awarded = " + ".join(p for p in parts if p)
    return OrderApproveResponse(
        order_id=order.id,
        order_number=order.order_number,
        new_status=OrderStatus.APPROVED.value,
        loyalty_points_awarded=standard + bonus,
        message=f"Order approved. {awarded} awarded." if awarded else "Order approved. No points earned for this order.",
        paid_at=order.paid_at,
    )


@router.post("/orders/{order_id}/ship", response_model=AdminOrderListItem)
async def ship_order(
    order_id: uuid.UUID,
    payload: ShipOrderRequest,
    background: BackgroundTasks,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    factory: async_sessionmaker = Depends(get_session_factory),
):
    """Hand a paid order to the carrier and tell the customer it is on its way.

    The dispatch e-mail and SMS are best-effort: with no SMTP server or SMS
    account configured they are only logged, and a send failure never undoes
    the dispatch.
    """
    order, user = await _order_with_user(db, order_id)
    if order.status != OrderStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=f"Only approved orders can be shipped — current status is {order.status.value}",
        )
    now = datetime.now(timezone.utc)
    carrier = payload.carrier or shipping.default_carrier(order)
    code = (payload.tracking_code or "").strip() or None
    link = None
    if payload.use_api:
        provider = shipping.provider(carrier, order.shipping_payer)
        if provider is None:
            raise HTTPException(status_code=409, detail="Bu kargo firması için web servis bağlantısı yok; takip numarasını elle girin.")
        key = shipping.next_cargo_key(order)
        try:
            await provider.create(shipping.shipment_request(order, key, user.email))
        except shipping.ShippingError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        order.cargo_key = key
        order.carrier_status, order.carrier_status_text = "NOP", "Gönderi kaydı açıldı; kargo şubeye teslim edilmeyi bekliyor."
        order.carrier_synced_at = now
        code = None  # Yurtiçi's own number comes with the sync, once the label is scanned.
        order.carrier = carrier
        # Until then the customer follows it by our reference.
        link = shipping.reference_tracking_url(order)
    elif order.carrier_status:
        # Shipped by hand after a web-service shipment was withdrawn: its status is not this one's.
        order.carrier_status = order.carrier_status_text = order.carrier_synced_at = None
    name = payload.carrier_name if carrier == Carrier.OTHER else None
    if carrier == Carrier.OTHER and not (name or "").strip():
        name = order.freight_company_name
    await order_lifecycle.mark_shipped(
        db, order, code, carrier=carrier, carrier_name=name, tracking_url=link or shipping.tracking_url(carrier, code), now=now,
    )
    await db.commit()
    await db.refresh(order, attribute_names=["items"])

    catalog = await _line_catalog(db, [order])
    lines = [
        (catalog.get(item.variant_id, (None, None, None, None))[2] or "Ürün", item.quantity)
        for item in order.items
    ]
    await mailer.send(order_emails.order_shipped(order, user.full_name, user.email, lines))
    background.add_task(order_sms.notify, factory, order.id, sms_settings.ORDER_SHIPPED)
    return _list_item(order, user, catalog)


@router.get("/shipping/integrations", response_model=ShippingIntegrationRead)
async def shipping_integrations(_admin: User = Depends(get_current_admin_user)):
    return ShippingIntegrationRead(
        yurtici_api=settings.yurtici_configured,
        yurtici_api_receiver=settings.yurtici_receiver_configured,
        yurtici_test_mode=settings.YURTICI_TEST_MODE,
        yurtici_customer_code=settings.yurtici_customer_code or None,
    )


async def _web_shipment(db: AsyncSession, order_id: uuid.UUID) -> tuple[Order, User, shipping.ShippingProvider]:
    order, user = await _order_with_user(db, order_id)
    if order.status != OrderStatus.SHIPPED or not order.cargo_key:
        raise HTTPException(status_code=409, detail="Bu sipariş web servis üzerinden kargolanmadı")
    provider = shipping.provider(order.carrier, order.shipping_payer)
    if provider is None:
        raise HTTPException(status_code=409, detail="Kargo firmasının web servis bağlantısı kapalı")
    return order, user, provider


@router.post("/orders/{order_id}/shipment/refresh", response_model=AdminOrderListItem)
async def refresh_shipment(
    order_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Ask the carrier now, rather than waiting for the hourly sync."""
    order, user, provider = await _web_shipment(db, order_id)
    try:
        [status] = await provider.query([order.cargo_key])
    except (shipping.ShippingError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc) or "Kargo firması yanıt vermedi") from exc
    shipping.apply_status(order, status, datetime.now(timezone.utc))
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    return _list_item(order, user, await _line_catalog(db, [order]))


@router.post("/orders/{order_id}/shipment/cancel", response_model=AdminOrderListItem)
async def cancel_shipment(
    order_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Kargo kaydını geri al": withdraw a web-service shipment the carrier has
    not collected yet, and put the order back to APPROVED to be shipped again.
    Once the parcel has been scanned it is the carrier's to return, not ours."""
    order, user, provider = await _web_shipment(db, order_id)
    if order.carrier_status != "NOP":
        raise HTTPException(status_code=409, detail="Kargo şubeye teslim edildi; kayıt artık geri alınamaz")
    try:
        status = await provider.cancel(order.cargo_key)
    except shipping.ShippingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    shipping.apply_status(order, status, datetime.now(timezone.utc))
    order.status = OrderStatus.APPROVED
    order.shipped_at = order.tracking_code = order.tracking_url = None
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    return _list_item(order, user, await _line_catalog(db, [order]))


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
