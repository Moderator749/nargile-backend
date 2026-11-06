""""WhatsApp ile Kurtar": a failed card payment (PAYMENT_FAILED) back as a bank
transfer at 5% off — its stock taken again, or nothing changed at all when the
goods have been sold since."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, ProductVariant, WarehouseType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import User
from app.services.order_tasks import cancel_expired_pending_orders
from tests.conftest import auth_header

QTY = 3


def _url(order_id) -> str:
    return f"/api/v1/admin/orders/{order_id}/recover-to-transfer"


async def _failed_card_order(
    db: AsyncSession,
    user: User,
    variant: ProductVariant,
    *,
    total: str = "3000.00",
    shipping: str = "0.00",
    discount: str = "0.00",
    lines: int = 1,
    status: OrderStatus = OrderStatus.PAYMENT_FAILED,
    **fields,
) -> Order:
    """A card order whose payment failed: its stock already went back (none is held here)."""
    order = Order(
        order_number=f"TST-{uuid.uuid4().hex[:10].upper()}",
        user_id=user.id,
        total_amount=Decimal(total),
        shipping_cost=Decimal(shipping),
        discount_amount=Decimal(discount),
        payment_method=PaymentMethod.CREDIT_CARD,
        status=status,
        cancel_reason="Kart limiti yetersiz",
        items=[
            OrderItem(
                variant_id=variant.id,
                quantity=QTY,
                unit_price=Decimal("1000.00"),
                line_total=Decimal("3000.00"),
                stock_warehouse=WarehouseType.RETAIL_STOCK,
            )
            for _ in range(lines)
        ],
        **fields,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order, attribute_names=["items"])
    return order


async def _stock(db: AsyncSession, variant_id) -> int:
    return await db.scalar(
        select(Inventory.quantity).where(
            Inventory.variant_id == variant_id, Inventory.warehouse_type == WarehouseType.RETAIL_STOCK
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


async def test_a_failed_card_order_becomes_a_transfer_at_five_percent_off(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _failed_card_order(db_session, customer_user, variant)
    before = await _stock(db_session, variant.id)

    resp = await client.post(_url(order.id), headers=auth_header(admin_user))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "AWAITING_TRANSFER"
    assert data["payment_method"] == "BANK_TRANSFER"
    assert Decimal(data["recovery_discount"]) == Decimal("150.00")
    assert Decimal(data["discount_amount"]) == Decimal("150.00")
    assert Decimal(data["total_amount"]) - Decimal(data["discount_amount"]) == Decimal("2850.00")
    assert data["recovered_at"] is not None
    # Why the card failed stays on record, for the history.
    assert data["cancel_reason"] == "Kart limiti yetersiz"
    # The goods are held again.
    assert await _stock(db_session, variant.id) == before - QTY


async def test_the_discount_is_five_percent_of_what_was_to_be_paid_to_the_kurus(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    # 1.600,00 goods − 48,48 already off + 0 carriage = 1.551,52 to pay → 77,576 → 77,58.
    order = await _failed_card_order(db_session, customer_user, variant, total="1600.00", discount="48.48")
    data = (await client.post(_url(order.id), headers=auth_header(admin_user))).json()
    assert Decimal(data["recovery_discount"]) == Decimal("77.58")
    assert Decimal(data["discount_amount"]) == Decimal("126.06")
    payable = Decimal(data["total_amount"]) + Decimal(data["shipping_cost"]) - Decimal(data["discount_amount"])
    assert payable == Decimal("1473.94")

    # Carriage is part of what is paid; and a half kuruş rounds up, not to even:
    # 100,00 + 0,10 → 5,005 → 5,01.
    order = await _failed_card_order(db_session, customer_user, variant, total="100.00", shipping="0.10")
    data = (await client.post(_url(order.id), headers=auth_header(admin_user))).json()
    assert Decimal(data["recovery_discount"]) == Decimal("5.01")


async def test_with_the_goods_sold_since_nothing_changes(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    # Two lines of three against four in stock: the first would fit, the second
    # does not — and the first's must not stay taken either.
    order = await _failed_card_order(db_session, customer_user, variant, lines=2)
    await _set_stock(db_session, variant, 4)
    # The refusal rolls the shared session back: read what it expires first.
    order_id, variant_id, headers = order.id, variant.id, auth_header(admin_user)

    resp = await client.post(_url(order_id), headers=headers)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith("Stok yetersiz, bu sipariş kurtarılamaz")
    assert "Normal Nargile" in detail and "stokta 1" in detail
    assert await _stock(db_session, variant_id) == 4
    db_session.expire_all()
    unchanged = await db_session.get(Order, order_id)
    assert unchanged.status == OrderStatus.PAYMENT_FAILED
    assert unchanged.payment_method == PaymentMethod.CREDIT_CARD
    assert unchanged.discount_amount == Decimal("0.00")
    assert unchanged.recovery_discount == Decimal("0.00")
    assert unchanged.recovered_at is None


async def test_only_a_failed_payment_and_only_once(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    for status in (OrderStatus.AWAITING_TRANSFER, OrderStatus.CANCELLED, OrderStatus.APPROVED):
        other = await _failed_card_order(db_session, customer_user, variant, status=status)
        assert (await client.post(_url(other.id), headers=auth_header(admin_user))).status_code == 409

    order = await _failed_card_order(db_session, customer_user, variant)
    order_id = order.id
    assert (await client.post(_url(order_id), headers=auth_header(admin_user))).status_code == 200
    again = await client.post(_url(order_id), headers=auth_header(admin_user))
    assert again.status_code == 409
    db_session.expire_all()
    assert (await db_session.get(Order, order_id)).discount_amount == Decimal("150.00")


async def test_only_an_admin_recovers(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _failed_card_order(db_session, customer_user, variant)
    assert (await client.post(_url(order.id), headers=auth_header(customer_user))).status_code == 403
    assert (await client.post(_url(order.id))).status_code == 401


async def test_the_transfer_gets_its_own_48_hours(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    # Placed four days ago: as a fresh transfer it is not cancelled for being old.
    order = await _failed_card_order(
        db_session, customer_user, variant, created_at=datetime.now(timezone.utc) - timedelta(days=4)
    )
    order_id = order.id
    assert (await client.post(_url(order_id), headers=auth_header(admin_user))).status_code == 200
    await cancel_expired_pending_orders(db_session)
    db_session.expire_all()
    assert (await db_session.get(Order, order_id)).status == OrderStatus.AWAITING_TRANSFER


# ── Kurtarmayı Geri Al ───────────────────────────────────────────────────────


def _revert(order_id) -> str:
    return f"/api/v1/admin/orders/{order_id}/revert-recovery"


async def test_reverting_puts_the_failed_payment_back_as_it_was(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    # Some discount of its own already (points, say), which must survive the round trip.
    order = await _failed_card_order(db_session, customer_user, variant, total="1600.00", discount="48.48")
    order_id, variant_id = order.id, variant.id
    before = await _stock(db_session, variant_id)
    assert (await client.post(_url(order_id), headers=auth_header(admin_user))).status_code == 200
    assert await _stock(db_session, variant_id) == before - QTY

    resp = await client.post(_revert(order_id), headers=auth_header(admin_user))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "PAYMENT_FAILED"
    assert data["payment_method"] == "CREDIT_CARD"
    assert Decimal(data["discount_amount"]) == Decimal("48.48")
    assert Decimal(data["recovery_discount"]) == Decimal("0.00")
    assert data["recovered_at"] is None
    assert data["cancel_reason"] == "Kart limiti yetersiz"
    # The goods are on the shelf again.
    assert await _stock(db_session, variant_id) == before


async def test_a_reverted_order_can_be_recovered_again_without_stacking(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _failed_card_order(db_session, customer_user, variant)
    order_id, headers = order.id, auth_header(admin_user)
    assert (await client.post(_url(order_id), headers=headers)).status_code == 200
    assert (await client.post(_revert(order_id), headers=headers)).status_code == 200
    # Once reverted, a second revert has nothing to undo.
    assert (await client.post(_revert(order_id), headers=headers)).status_code == 409
    again = await client.post(_url(order_id), headers=headers)
    assert again.status_code == 200
    assert Decimal(again.json()["discount_amount"]) == Decimal("150.00")


async def test_only_an_unpaid_recovered_transfer_is_reverted(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    headers = auth_header(admin_user)
    # A failed payment never recovered, and an ordinary transfer (no recovery discount).
    failed = await _failed_card_order(db_session, customer_user, variant)
    plain = await _failed_card_order(db_session, customer_user, variant, status=OrderStatus.AWAITING_TRANSFER)
    failed_id, plain_id = failed.id, plain.id
    assert (await client.post(_revert(failed_id), headers=headers)).status_code == 409
    resp = await client.post(_revert(plain_id), headers=headers)
    assert resp.status_code == 409
    assert "kurtarılmış" in resp.json()["detail"]

    # Paid meanwhile: the money is in, there is nothing to undo.
    paid = await _failed_card_order(db_session, customer_user, variant)
    paid_id = paid.id
    assert (await client.post(_url(paid_id), headers=headers)).status_code == 200
    assert (await client.post(f"/api/v1/admin/orders/{paid_id}/approve", headers=headers)).status_code == 200
    resp = await client.post(_revert(paid_id), headers=headers)
    assert resp.status_code == 409

    # The store has released it for packing: that is taken back first.
    packed = await _failed_card_order(db_session, customer_user, variant)
    packed_id = packed.id
    assert (await client.post(_url(packed_id), headers=headers)).status_code == 200
    approval = await client.post(f"/api/v1/admin/orders/{packed_id}/store-approval", json={"approved": True}, headers=headers)
    assert approval.status_code == 200, approval.text
    resp = await client.post(_revert(packed_id), headers=headers)
    assert resp.status_code == 409
    assert "mağaza onayı" in resp.json()["detail"].lower()
    db_session.expire_all()
    assert (await db_session.get(Order, packed_id)).status == OrderStatus.AWAITING_TRANSFER


async def test_only_an_admin_reverts(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    variant, _ = catalog
    order = await _failed_card_order(db_session, customer_user, variant)
    order_id = order.id
    assert (await client.post(_url(order_id), headers=auth_header(admin_user))).status_code == 200
    assert (await client.post(_revert(order_id), headers=auth_header(customer_user))).status_code == 403
    assert (await client.post(_revert(order_id))).status_code == 401
