"""Sipariş Notu: the customer's own message on an order.

Optional, free text, and nobody's business but the person who picks the order
up — so what these tests pin is that it survives the round trip exactly as it
was typed, that an order without one is simply an order without one, and that
the panel sees it either way.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.order import Order
from app.models.user import AccountType, CustomerSegment, SegmentType, User, UserRole
from tests.conftest import ADDRESS, CONSENTS, auth_header

pytestmark = pytest.mark.asyncio

PRICE = Decimal("5000.00")  # over the free-shipping threshold: no consent in the way
NOTE = "Lütfen teslimattan önce arayın.\nKapıcıya bırakmayın."


async def _variant(db: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"NOT-{suffix}", name=f"Not Testi {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"NOT-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=PRICE, vat_rate=Decimal("20.00"),
        ))
    for warehouse in WarehouseType:
        db.add(Inventory(variant_id=variant.id, warehouse_type=warehouse, quantity=20))
    await db.commit()
    return variant


async def _buyer(
    db: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
    account_type: AccountType = AccountType.INDIVIDUAL,
    segment: SegmentType = SegmentType.RETAIL,
) -> User:
    user = User(
        email=f"note-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        full_name="Not Alıcısı",
        phone="0532 111 22 33",
        role=UserRole.CUSTOMER,
        account_type=account_type,
        segment_id=segments[segment].id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _body(variant: ProductVariant, **extra) -> dict:
    return {
        **CONSENTS,
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": ADDRESS,
        **extra,
    }


async def test_a_note_reaches_the_order_as_it_was_typed(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, order_note=f"  {NOTE}  "),
    )
    assert placed.status_code == 201, placed.text
    # Trimmed at the ends; the line break inside is the customer's own.
    assert placed.json()["order_note"] == NOTE

    stored = await db_session.scalar(select(Order).where(Order.id == uuid.UUID(placed.json()["id"])))
    await db_session.refresh(stored)
    assert stored.order_note == NOTE

    # The panel's "SİPARİŞ NOTU" block reads it from here.
    rows = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in rows.json() if o["id"] == placed.json()["id"])
    assert row["order_note"] == NOTE

    # And the customer sees their own note on their own order.
    mine = await client.get(f"/api/v1/orders/{placed.json()['id']}", headers=auth_header(user))
    assert mine.json()["order_note"] == NOTE


@pytest.mark.parametrize(
    "note", [pytest.param(None, id="not sent"), pytest.param("   \n  ", id="whitespace only")]
)
async def test_an_order_without_a_note_has_none(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User, note: str | None
):
    """Nothing to say is not an error, and not an empty string either: the
    panel's dash is what null looks like."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    body = _body(variant) if note is None else _body(variant, order_note=note)
    placed = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)
    assert placed.status_code == 201, placed.text
    assert placed.json()["order_note"] is None

    rows = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in rows.json() if o["id"] == placed.json()["id"])
    assert row["order_note"] is None


async def test_the_sheet_can_re_read_one_order(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User, customer_user: User
):
    """The order sheet reads its own order when it opens, so a note written
    after the listing was fetched is still the note it shows."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)

    # The listing as the panel had it before this order existed.
    before = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    placed = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user), json=_body(variant, order_note=NOTE)
    )
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]
    assert all(row["id"] != order_id for row in before.json())

    one = await client.get(f"/api/v1/admin/orders/{order_id}", headers=auth_header(admin_user))
    assert one.status_code == 200, one.text
    assert one.json()["order_note"] == NOTE
    # The same shape as a listing row, so the sheet can swap one for the other.
    assert set(one.json()) == set(before.json()[0]) if before.json() else True
    assert one.json()["items"] and one.json()["customer_email"] == user.email

    # It is the panel's own door: a customer cannot read orders through it.
    assert (
        await client.get(f"/api/v1/admin/orders/{order_id}", headers=auth_header(customer_user))
    ).status_code == 403
    assert (await client.get(f"/api/v1/admin/orders/{order_id}")).status_code == 401
    unknown = await client.get(
        f"/api/v1/admin/orders/{uuid.uuid4()}", headers=auth_header(admin_user)
    )
    assert unknown.status_code == 404


async def test_a_note_too_long_to_read_is_refused(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    refused = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, order_note="a" * 501),
    )
    assert refused.status_code == 422, refused.text
    # 500 exactly is still a note.
    accepted = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, order_note="a" * 500),
    )
    assert accepted.status_code == 201, accepted.text
    assert len(accepted.json()["order_note"]) == 500


async def test_a_firm_can_leave_a_note_too(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Every flow carries it: the field is on the request every account sends."""
    user = await _buyer(
        db_session, segments, account_type=AccountType.CAFE, segment=SegmentType.CAFE
    )
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, shipping_method="NAKLIYE_AMBARI", order_note="Ambara teslim edilsin."),
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["order_note"] == "Ambara teslim edilsin."


async def test_a_price_preview_ignores_the_note(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """It is not a price, so a cart that carries one is quoted the same."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    quote = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(user),
        json={
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": str(variant.id), "quantity": 1}],
            "order_note": NOTE,
        },
    )
    assert quote.status_code == 200, quote.text
    assert Decimal(quote.json()["net_total"]) == PRICE
