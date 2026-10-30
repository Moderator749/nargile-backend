"""The order lifecycle's newer steps: "Siparişi Kurtar", "Mağazadan Teslim",
İade (RETURNED), the carrier's delivery (DELIVERED) and the "Gizli Mağaza
Notu" no customer ever sees."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, ProductVariant, WarehouseType
from app.models.order import VOID_STATUSES, Carrier, Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import User
from app.services import shipping
from app.services.order_tasks import cancel_expired_pending_orders
from app.services.shipping.base import ShipmentStatus
from tests.conftest import auth_header

QTY = 3


async def _order(
    db: AsyncSession, user: User, variant: ProductVariant, status: OrderStatus, **fields
) -> Order:
    order = Order(
        order_number=f"TST-{uuid.uuid4().hex[:10].upper()}",
        user_id=user.id,
        total_amount=Decimal("3000.00"),
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=status,
        items=[
            OrderItem(
                variant_id=variant.id,
                quantity=QTY,
                unit_price=Decimal("1000.00"),
                line_total=Decimal("3000.00"),
                stock_warehouse=WarehouseType.RETAIL_STOCK,
            )
        ],
        **fields,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    return order


async def _stock(db: AsyncSession, variant: ProductVariant) -> int:
    # A column read goes to the database, whatever the session holds.
    return await db.scalar(
        select(Inventory.quantity).where(
            Inventory.variant_id == variant.id, Inventory.warehouse_type == WarehouseType.RETAIL_STOCK
        )
    )


async def _set_stock(db: AsyncSession, variant: ProductVariant, quantity: int) -> None:
    inv = await db.scalar(
        select(Inventory).where(
            Inventory.variant_id == variant.id, Inventory.warehouse_type == WarehouseType.RETAIL_STOCK
        )
    )
    inv.quantity = quantity
    await db.commit()


# ── Siparişi Kurtar ──────────────────────────────────────────────────────────


async def test_a_cancelled_transfer_order_is_recovered_waiting_for_the_money(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.AWAITING_TRANSFER)
    before = await _stock(db_session, variant)

    cancel = await client.post(
        f"/api/v1/admin/orders/{order.id}/cancel", json={"reason": "48 saat doldu"}, headers=auth_header(admin_user)
    )
    assert cancel.status_code == 200, cancel.text
    assert await _stock(db_session, variant) == before + QTY

    resp = await client.post(f"/api/v1/admin/orders/{order.id}/recover", json={}, headers=auth_header(admin_user))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "AWAITING_TRANSFER"
    assert data["recovered_at"] is not None
    # The cancellation stays on record, for the history.
    assert data["cancel_reason"] == "48 saat doldu"
    assert await _stock(db_session, variant) == before


async def test_a_recovered_order_paid_meanwhile_is_approved_at_once(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.AWAITING_TRANSFER)
    await client.post(f"/api/v1/admin/orders/{order.id}/cancel", json={"reason": "süre doldu"}, headers=auth_header(admin_user))

    resp = await client.post(f"/api/v1/admin/orders/{order.id}/recover", json={"paid": True}, headers=auth_header(admin_user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["paid_at"] is not None


async def test_recovery_refuses_when_the_goods_have_been_sold_since(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.AWAITING_TRANSFER)
    await client.post(f"/api/v1/admin/orders/{order.id}/cancel", json={"reason": "süre doldu"}, headers=auth_header(admin_user))
    await _set_stock(db_session, variant, 1)
    # The refusal rolls the shared session back: read what it expires first.
    order_id, variant_id, headers = order.id, variant.id, auth_header(admin_user)

    resp = await client.post(f"/api/v1/admin/orders/{order_id}/recover", json={"paid": True}, headers=headers)
    assert resp.status_code == 409
    assert "Normal Nargile" in resp.json()["detail"]
    stock = await db_session.scalar(
        select(Inventory.quantity).where(
            Inventory.variant_id == variant_id, Inventory.warehouse_type == WarehouseType.RETAIL_STOCK
        )
    )
    assert stock == 1
    assert (await db_session.get(Order, order_id)).status == OrderStatus.CANCELLED


async def test_only_a_cancelled_order_can_be_recovered(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.post(f"/api/v1/admin/orders/{order.id}/recover", json={}, headers=auth_header(admin_user))
    assert resp.status_code == 409


async def test_a_recovered_transfer_gets_a_fresh_48_hours(
    db_session: AsyncSession, customer_user: User, catalog
):
    variant, _ = catalog
    now = datetime.now(timezone.utc)
    order = await _order(
        db_session, customer_user, variant, OrderStatus.AWAITING_TRANSFER,
        created_at=now - timedelta(days=4), recovered_at=now - timedelta(hours=1),
    )
    order_id = order.id
    await cancel_expired_pending_orders(db_session)
    db_session.expire_all()
    assert (await db_session.get(Order, order_id)).status == OrderStatus.AWAITING_TRANSFER


# ── Mağazadan Teslim ─────────────────────────────────────────────────────────


async def test_an_approved_order_is_handed_over_at_the_shop(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.post(f"/api/v1/admin/orders/{order.id}/deliver-in-store", headers=auth_header(admin_user))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "DELIVERED"
    assert data["carrier"] == "STORE"
    assert data["delivered_at"] is not None
    assert data["tracking_code"] is None


async def test_only_an_approved_order_is_handed_over(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.AWAITING_TRANSFER)
    resp = await client.post(f"/api/v1/admin/orders/{order.id}/deliver-in-store", headers=auth_header(admin_user))
    assert resp.status_code == 409


async def test_kargola_does_not_take_the_shop_as_a_carrier(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/ship", json={"carrier": "STORE"}, headers=auth_header(admin_user)
    )
    assert resp.status_code == 422


# ── Teslim edildi (the carrier's word) ───────────────────────────────────────


async def test_the_carriers_delivery_makes_the_order_delivered(db_session: AsyncSession, customer_user: User, catalog):
    variant, _ = catalog
    order = await _order(
        db_session, customer_user, variant, OrderStatus.SHIPPED, carrier=Carrier.YURTICI, cargo_key="TSTKEY1"
    )
    now = datetime.now(timezone.utc)
    shipping.apply_status(order, ShipmentStatus(cargo_key="TSTKEY1", code="DLV", text="Teslim edildi", delivered=True), now)
    assert order.status == OrderStatus.DELIVERED
    assert order.delivered_at == now


# ── İade ─────────────────────────────────────────────────────────────────────


async def test_a_delivered_order_is_returned_and_restocked(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.DELIVERED)
    before = await _stock(db_session, variant)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/return", json={"reason": "Müşteri beğenmedi"}, headers=auth_header(admin_user)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "RETURNED"
    assert data["return_reason"] == "Müşteri beğenmedi"
    assert data["returned_at"] is not None
    assert data["cancelled_at"] is None
    assert await _stock(db_session, variant) == before + QTY
    # Its points settle as on a cancellation.
    assert OrderStatus.RETURNED in VOID_STATUSES


async def test_a_damaged_return_stays_off_the_shelf(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.SHIPPED)
    before = await _stock(db_session, variant)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/return",
        json={"reason": "Kırık geldi", "restock": False},
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200, resp.text
    assert await _stock(db_session, variant) == before


async def test_an_order_that_never_left_is_cancelled_not_returned(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/return", json={"reason": "vazgeçti"}, headers=auth_header(admin_user)
    )
    assert resp.status_code == 409


# ── Gizli Mağaza Notu ────────────────────────────────────────────────────────


async def test_the_shops_note_is_saved_and_never_reaches_the_customer(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.put(
        f"/api/v1/admin/orders/{order.id}/internal-note",
        json={"internal_note": "  Müşteri sorunlu, kapıda ödeme isteme  "},
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["internal_note"] == "Müşteri sorunlu, kapıda ödeme isteme"

    admin_view = await client.get(f"/api/v1/admin/orders/{order.id}", headers=auth_header(admin_user))
    assert admin_view.json()["internal_note"] == "Müşteri sorunlu, kapıda ödeme isteme"

    mine = await client.get(f"/api/v1/orders/{order.id}", headers=auth_header(customer_user))
    assert mine.status_code == 200, mine.text
    assert "internal_note" not in mine.json()
    listing = await client.get("/api/v1/orders", headers=auth_header(customer_user))
    assert all("internal_note" not in row for row in listing.json())

    cleared = await client.put(
        f"/api/v1/admin/orders/{order.id}/internal-note", json={"internal_note": "  "}, headers=auth_header(admin_user)
    )
    assert cleared.json()["internal_note"] is None


async def test_a_customer_cannot_write_the_shops_note(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.put(
        f"/api/v1/admin/orders/{order.id}/internal-note", json={"internal_note": "x"}, headers=auth_header(customer_user)
    )
    assert resp.status_code == 403


async def test_status_tabs_count_the_new_statuses(client: AsyncClient, admin_user: User):
    resp = await client.get("/api/v1/admin/orders/status-counts", headers=auth_header(admin_user))
    assert resp.status_code == 200
    assert {"DELIVERED", "RETURNED"} <= set(resp.json())


# ── Mağaza Onayı ─────────────────────────────────────────────────────────────


async def test_the_store_approves_an_order_apart_from_its_payment(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.AWAITING_TRANSFER)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": True}, headers=auth_header(admin_user)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["store_approved_at"] is not None
    assert data["store_approved_by_name"] == (admin_user.username or admin_user.full_name or admin_user.email)
    # The payment status is its own business.
    assert data["status"] == "AWAITING_TRANSFER"
    # Approving again keeps the first approval.
    again = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": True}, headers=auth_header(admin_user)
    )
    assert again.json()["store_approved_at"] == data["store_approved_at"]
    # Taken back.
    back = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": False}, headers=auth_header(admin_user)
    )
    assert back.json()["store_approved_at"] is None and back.json()["store_approved_by_name"] is None
    # The customer never sees it.
    mine = await client.get(f"/api/v1/orders/{order.id}", headers=auth_header(customer_user))
    assert "store_approved_at" not in mine.json()


async def test_a_closed_order_takes_no_store_approval(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.CANCELLED)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": True}, headers=auth_header(admin_user)
    )
    assert resp.status_code == 409


async def test_a_shipped_orders_store_approval_stays(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    await client.post(f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": True}, headers=auth_header(admin_user))
    await client.post(f"/api/v1/admin/orders/{order.id}/deliver-in-store", headers=auth_header(admin_user))
    back = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": False}, headers=auth_header(admin_user)
    )
    assert back.status_code == 409


async def test_only_an_admin_gives_store_approval(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.APPROVED)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": True}, headers=auth_header(customer_user)
    )
    assert resp.status_code == 403


async def test_an_order_that_left_unapproved_takes_no_store_approval(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _order(db_session, customer_user, variant, OrderStatus.SHIPPED)
    resp = await client.post(
        f"/api/v1/admin/orders/{order.id}/store-approval", json={"approved": True}, headers=auth_header(admin_user)
    )
    assert resp.status_code == 409
