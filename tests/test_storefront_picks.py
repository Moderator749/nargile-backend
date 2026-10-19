"""The homepage's "Sizin İçin Seçtiklerimiz" tabs: "Yeni Gelenler" (newest first)
and "Çok Satanlar" (by the quantity sold on approved or shipped orders)."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductVariant
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import User

# Far above anything the rest of the suite sells, so these lead the list.
BIG = 1_000_000


@pytest.fixture
async def picks(db_session: AsyncSession, customer_user: User):
    """Four products, each with two colours; the caller adds the orders."""
    suffix = uuid.uuid4().hex[:6]
    # Dated ahead of every other product in the suite, so they lead "newest".
    soon = datetime.now(timezone.utc) + timedelta(days=30)
    products = [
        Product(base_code=f"PCK{i}-{suffix}", name=f"Seçki {i} {suffix}", tags=[], created_at=soon + timedelta(hours=i))
        for i in range(4)
    ]
    db_session.add_all(products)
    await db_session.flush()
    variants = {
        p.id: [ProductVariant(product_id=p.id, sku=f"{p.base_code}-{c}", color_name=c) for c in ("Siyah", "Gri")]
        for p in products
    }
    db_session.add_all(v for vs in variants.values() for v in vs)
    await db_session.commit()
    orders: list[Order] = []

    async def sell(lines: list[tuple[ProductVariant, int]], status=OrderStatus.APPROVED, is_quote=False):
        order = Order(
            order_number=f"P-{uuid.uuid4().hex[:10]}", user_id=customer_user.id,
            total_amount=Decimal("1"), shipping_cost=Decimal("0"), discount_amount=Decimal("0"),
            payment_method=PaymentMethod.QUOTE if is_quote else PaymentMethod.BANK_TRANSFER,
            status=status, is_quote=is_quote,
        )
        db_session.add(order)
        await db_session.flush()
        db_session.add_all(
            OrderItem(order_id=order.id, variant_id=v.id, quantity=q, unit_price=Decimal("1"), line_total=Decimal(q))
            for v, q in lines
        )
        await db_session.commit()
        orders.append(order)

    yield products, variants, sell

    order_ids = [o.id for o in orders]
    await db_session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
    await db_session.execute(delete(Order).where(Order.id.in_(order_ids)))
    ids = [p.id for p in products]
    await db_session.execute(delete(ProductVariant).where(ProductVariant.product_id.in_(ids)))
    await db_session.execute(delete(Product).where(Product.id.in_(ids)))
    await db_session.commit()


async def _ids(client: AsyncClient, path: str, **params) -> list[str]:
    resp = await client.get(f"/api/v1/products{path}", params=params)
    assert resp.status_code == 200, resp.text
    return [p["id"] for p in resp.json()]


async def test_newest_lists_the_latest_added_first(client: AsyncClient, db_session: AsyncSession, picks):
    products, *_ = picks
    products[1].is_published = False
    await db_session.commit()
    newest = await _ids(client, "", sort="newest", limit=3)
    # Latest first; the unpublished one is not on the storefront at all.
    assert newest == [str(products[3].id), str(products[2].id), str(products[0].id)]


async def test_unknown_sort_is_rejected(client: AsyncClient):
    resp = await client.get("/api/v1/products", params={"sort": "price"})
    assert resp.status_code == 422


async def test_bestsellers_rank_by_quantity_sold_across_colours(client: AsyncClient, picks):
    products, variants, sell = picks
    a, b, c, _ = products
    # a: 3 BIG over two colours and two orders (one shipped); b: 2 BIG; c: BIG.
    await sell([(variants[a.id][0], BIG), (variants[b.id][0], 2 * BIG)])
    await sell([(variants[a.id][1], 2 * BIG), (variants[c.id][0], BIG)], status=OrderStatus.SHIPPED)
    ranked = await _ids(client, "/bestsellers", limit=3)
    assert ranked == [str(a.id), str(b.id), str(c.id)]
    assert await _ids(client, "/bestsellers", limit=1) == [str(a.id)]


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.PENDING_APPROVAL,
        OrderStatus.AWAITING_TRANSFER,
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.CANCELLED,
        OrderStatus.PAYMENT_FAILED,
    ],
)
async def test_bestsellers_count_only_approved_or_shipped_orders(client: AsyncClient, picks, status):
    products, variants, sell = picks
    await sell([(variants[products[0].id][0], 10 * BIG)], status=status)
    assert str(products[0].id) not in await _ids(client, "/bestsellers", limit=50)


async def test_bestsellers_leave_out_quotes_and_unpublished_products(
    client: AsyncClient, db_session: AsyncSession, picks
):
    products, variants, sell = picks
    quoted, hidden, sold, _ = products
    await sell([(variants[quoted.id][0], 10 * BIG)], is_quote=True)
    await sell([(variants[hidden.id][0], 10 * BIG), (variants[sold.id][0], BIG)])
    hidden.is_published = False
    await db_session.commit()
    ranked = await _ids(client, "/bestsellers", limit=50)
    assert str(sold.id) in ranked
    assert str(quoted.id) not in ranked
    assert str(hidden.id) not in ranked


async def test_bestsellers_limit_is_bounded(client: AsyncClient):
    assert (await client.get("/api/v1/products/bestsellers", params={"limit": 51})).status_code == 422
