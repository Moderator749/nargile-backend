"""The export customer's order request.

An INTERNATIONAL account has no payment screen. It builds a cart at export
prices and asks for it to be confirmed: the freight, the customs and the total
are agreed afterwards, against the transport type it picked. So its "order" is
a request, and these tests hold the three things that follow from that —
nothing is charged, nothing leaves the warehouse, and the quantities it asks
for are not limited to what happens to be in stock today.

The guard runs both ways: a domestic account cannot submit a request, and an
export account cannot submit anything else.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice
from app.models.order import Order, PaymentMethod
from app.models.user import CustomerSegment, SegmentType
from app.services.checkout_engine import warehouse_for_segment
from tests.conftest import CONSENTS

UNIT_PRICE = Decimal("1000.00")
IN_STOCK = 3

ABROAD = {
    "full_name": "John Smith",
    "phone": "+49 30 1234 5678",
    "country": "DE",
    "city": "Berlin",
    "postal_code": "10115",
    "line": "Hauptstraße 1, Apt 5",
}


async def _account(client: AsyncClient, *, account_type: str = "INTERNATIONAL") -> dict[str, str]:
    email = f"exp-{uuid.uuid4().hex[:8]}@test.com"
    body: dict = {
        "account_type": account_type,
        "email": email,
        "password": "Gizli-Parola-123",
        "contact_name": "John Smith",
        "phone": "+49 30 1234 5678",
    }
    if account_type == "INTERNATIONAL":
        body["international_kind"] = "WHOLESALER"
        body["company"] = {"entity_type": "COMPANY", "title": "Berlin Shisha GmbH"}
        body["billing_address"] = {"country": "DE", "city": "Berlin", "address_line1": "Hauptstraße 1"}
    else:
        body["company"] = {
            "entity_type": "COMPANY", "title": "Köşe Kafe Ltd.", "tax_plate_name": "Köşe Kafe Ltd.",
            "tax_office": "Beşiktaş", "tax_number": "1234567890",
        }
        body["billing_address"] = {
            "neighbourhood": "Caferağa Mah.", "street": "Moda Cad.", "building_no": "7",
            "district": "Kadıköy", "city": "İstanbul",
        }
    registered = await client.post("/api/v1/auth/register", json=body)
    assert registered.status_code == 201, registered.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _variant(
    db: AsyncSession, segments: dict[SegmentType, CustomerSegment]
) -> ProductVariant:
    """Priced on every list, and deliberately short of stock: three units."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"EXP-{suffix}", name=f"Export Testi {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"EXP-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(UNIT_PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=UNIT_PRICE, vat_rate=Decimal("20.00"),
        ))
        db.add(Inventory(
            variant_id=variant.id, warehouse_type=warehouse_for_segment(code), quantity=IN_STOCK,
        ))
    await db.commit()
    return variant


def _request(variant: ProductVariant, quantity: int = 1, **extra) -> dict:
    return {
        **CONSENTS,
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": quantity}],
        "shipping_method": "INTERNATIONAL_FREIGHT",
        "shipping_consent": True,
        "shipping_address": ABROAD,
        "transport_mode": "SEA",
        **extra,
    }


async def _stock(db: AsyncSession, variant_id: uuid.UUID) -> list[int]:
    """Read by id: after `expire_all` the variant object would reload lazily,
    which is IO in the wrong place under an async session."""
    rows = await db.execute(select(Inventory.quantity).where(Inventory.variant_id == variant_id))
    return sorted(rows.scalars().all())


# ── What an export order is ──────────────────────────────────────────────────


async def test_an_export_order_is_a_request_not_a_payment(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """No gateway was used, so the order records none — and says what it is."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    placed = await client.post("/api/v1/orders/checkout", headers=headers, json=_request(variant))
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["is_quote"] is True
    assert body["payment_method"] == "QUOTE"
    assert body["transport_mode"] == "SEA"
    # A request waits for a person, not for money.
    assert body["status"] == "PENDING_APPROVAL"


@pytest.mark.parametrize("mode", ["ROAD", "AIR", "SEA"])
async def test_each_transport_type_is_kept_as_asked(
    client: AsyncClient, db_session: AsyncSession, segments, mode
):
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=headers, json=_request(variant, transport_mode=mode)
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["transport_mode"] == mode


async def test_a_request_without_a_transport_type_is_refused(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """It is the one thing the freight will be quoted against."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    body = _request(variant)
    body.pop("transport_mode")
    refused = await client.post("/api/v1/orders/checkout", headers=headers, json=body)
    assert refused.status_code == 422
    assert "transport type" in refused.json()["detail"]


# ── Stock ────────────────────────────────────────────────────────────────────


async def test_a_request_may_ask_for_more_than_is_in_stock(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Three on the shelf, five hundred asked for: that is the question being
    put to the shop, and refusing it at checkout would be answering it."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=headers, json=_request(variant, quantity=500)
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["items"][0]["quantity"] == 500


async def test_a_request_leaves_the_warehouse_alone(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    variant_id = variant.id
    before = await _stock(db_session, variant_id)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=headers, json=_request(variant, quantity=2)
    )
    assert placed.status_code == 201, placed.text
    db_session.expire_all()
    assert await _stock(db_session, variant_id) == before == [IN_STOCK] * len(before)


async def test_a_domestic_order_still_cannot_exceed_its_stock(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The bypass belongs to the request, not to everyone."""
    headers = await _account(client, account_type="CAFE")
    variant = await _variant(db_session, segments)
    refused = await client.post("/api/v1/orders/checkout", headers=headers, json={
        **CONSENTS,
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": IN_STOCK + 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": {
            "full_name": "Mehmet Demir", "phone": "0532 444 55 66", "country": "TR",
            "city": "İstanbul", "district": "Kadıköy", "line": "Moda Cad. No: 7",
        },
    })
    assert refused.status_code == 422
    assert "stock" in refused.json()["detail"].lower()


async def test_a_domestic_order_still_takes_its_stock(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _account(client, account_type="CAFE")
    variant = await _variant(db_session, segments)
    variant_id = variant.id
    placed = await client.post("/api/v1/orders/checkout", headers=headers, json={
        **CONSENTS,
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": {
            "full_name": "Mehmet Demir", "phone": "0532 444 55 66", "country": "TR",
            "city": "İstanbul", "district": "Kadıköy", "line": "Moda Cad. No: 7",
        },
    })
    assert placed.status_code == 201, placed.text
    db_session.expire_all()
    # One warehouse is one unit lighter; the others are untouched.
    assert await _stock(db_session, variant_id) == sorted([IN_STOCK - 1] + [IN_STOCK] * 3)


# ── The guard, both ways ─────────────────────────────────────────────────────


async def test_a_domestic_account_cannot_submit_a_quote(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _account(client, account_type="CAFE")
    variant = await _variant(db_session, segments)
    refused = await client.post("/api/v1/orders/checkout", headers=headers, json={
        **CONSENTS,
        "payment_method": "QUOTE",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": {
            "full_name": "Mehmet Demir", "phone": "0532 444 55 66", "country": "TR",
            "city": "İstanbul", "district": "Kadıköy", "line": "Moda Cad. No: 7",
        },
    })
    assert refused.status_code == 422
    assert "ödeme yöntemi" in refused.json()["detail"]


async def test_a_domestic_account_cannot_choose_a_transport_type(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _account(client, account_type="CAFE")
    variant = await _variant(db_session, segments)
    refused = await client.post("/api/v1/orders/checkout", headers=headers, json={
        **CONSENTS,
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "transport_mode": "AIR",
        "shipping_address": {
            "full_name": "Mehmet Demir", "phone": "0532 444 55 66", "country": "TR",
            "city": "İstanbul", "district": "Kadıköy", "line": "Moda Cad. No: 7",
        },
    })
    assert refused.status_code == 422
    assert "yurt dışı" in refused.json()["detail"]


async def test_an_export_requests_payment_choice_is_ignored(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """It may send CREDIT_CARD; what is stored is still a request."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=headers,
        json=_request(variant, payment_method="CREDIT_CARD"),
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["payment_method"] == "QUOTE"


async def test_the_note_and_the_request_are_stored_together(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The note is where the customer says who they are and what they want."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    note = "We are a wholesaler in Berlin. Please quote 20 cartons, sea freight."
    placed = await client.post(
        "/api/v1/orders/checkout", headers=headers, json=_request(variant, order_note=note)
    )
    assert placed.status_code == 201, placed.text
    stored = await db_session.scalar(
        select(Order).where(Order.id == uuid.UUID(placed.json()["id"]))
    )
    assert stored.order_note == note
    assert stored.is_quote is True
    assert stored.payment_method == PaymentMethod.QUOTE


# ── What a request is not asked for ──────────────────────────────────────────


async def test_a_request_needs_neither_agreement(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The agreements make an order a contract. A request is not one yet."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    body = _request(variant)
    body["distance_sales_accepted"] = False
    body["terms_accepted"] = False
    placed = await client.post("/api/v1/orders/checkout", headers=headers, json=body)
    assert placed.status_code == 201, placed.text
    assert placed.json()["agreements_accepted_at"] is None


async def test_a_request_needs_no_carriage_consent(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Nobody has named a freight price yet, so there is nothing to consent to."""
    headers = await _account(client)
    variant = await _variant(db_session, segments)
    body = _request(variant)
    body["shipping_consent"] = False
    placed = await client.post("/api/v1/orders/checkout", headers=headers, json=body)
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_consent_at"] is None


async def test_an_order_still_needs_both_agreements(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The exemption belongs to the request, not to everyone."""
    headers = await _account(client, account_type="CAFE")
    variant = await _variant(db_session, segments)
    refused = await client.post("/api/v1/orders/checkout", headers=headers, json={
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "distance_sales_accepted": False,
        "terms_accepted": True,
        "shipping_address": {
            "full_name": "Mehmet Demir", "phone": "0532 444 55 66", "country": "TR",
            "city": "İstanbul", "district": "Kadıköy", "line": "Moda Cad. No: 7",
        },
    })
    assert refused.status_code == 422
    assert "Mesafeli Satış" in refused.json()["detail"]
