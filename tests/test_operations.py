"""Havale/EFT flow, Istanbul courier, ParaPuan/Kumbarapuan wallet, admin controls."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_account import BankAccount
from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order, OrderStatus
from app.models.user import SegmentType, User
from app.schemas.wallet import normalise_tr_iban
from app.services.wallet import get_wallet
from tests.conftest import ADDRESS, auth_header

ISTANBUL_ADDRESS = {**ADDRESS, "city": "istanbul", "district": "Kadıköy"}
VALID_IBAN = "TR33 0006 1005 1978 6457 8413 26"  # checksum-valid test IBAN


async def _checkout(client, user, variant, qty=1, **extra):
    body = {
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": qty}],
        "shipping_address": ADDRESS,
        # Harmless when the shop covers carriage; required when the receiver pays.
        "shipping_consent": True,
        **extra,
    }
    return await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)


async def _seed_cheap_variant(db: AsyncSession, retail_segment_id: int) -> ProductVariant:
    """A ₺250 product, so the cart stays under the free-shipping threshold."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"CHP-{suffix}", name="Küçük Aksesuar", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"CHP-{suffix}")
    db.add(variant)
    await db.flush()
    db.add(VariantPrice(
        variant_id=variant.id, segment_id=retail_segment_id, currency="TRY",
        price_excl_tax=Decimal("211.86"), price_incl_tax=Decimal("250.00"), vat_rate=Decimal("18.00"),
    ))
    db.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=100))
    await db.commit()
    return variant


async def _approve(client, admin, order_id):
    return await client.post(f"/api/v1/admin/orders/{order_id}/approve", headers=auth_header(admin))


# ── Havale / EFT ─────────────────────────────────────────────────────────────


async def test_bank_transfer_order_awaits_transfer_with_address(client: AsyncClient, customer_user: User, catalog):
    normal_v, _ = catalog
    resp = await _checkout(client, customer_user, normal_v)
    assert resp.status_code == 201, resp.text
    order = resp.json()
    assert order["status"] == "AWAITING_TRANSFER"
    assert order["shipping_method"] == "CARGO"
    assert order["shipping_address"]["city"] == "Ankara"


async def test_card_order_stays_pending_approval(client: AsyncClient, customer_user: User, catalog):
    normal_v, _ = catalog
    resp = await _checkout(client, customer_user, normal_v, payment_method="CREDIT_CARD")
    assert resp.json()["status"] == "PENDING_APPROVAL"


async def test_checkout_requires_an_address(client: AsyncClient, customer_user: User, catalog):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={"payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(normal_v.id), "quantity": 1}]},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "address",
    [
        {**ADDRESS, "city": "Atlantis"},
        {**ADDRESS, "phone": "123"},
        {**ADDRESS, "line": "x"},
    ],
)
async def test_address_validation(client: AsyncClient, customer_user: User, catalog, address: dict):
    normal_v, _ = catalog
    assert (await _checkout(client, customer_user, normal_v, shipping_address=address)).status_code == 422


async def test_admin_marks_transfer_paid(client: AsyncClient, admin_user: User, customer_user: User, catalog):
    normal_v, _ = catalog
    order = (await _checkout(client, customer_user, normal_v)).json()
    resp = await _approve(client, admin_user, order["id"])
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "APPROVED"
    assert (await _approve(client, admin_user, order["id"])).status_code == 409


async def test_admin_order_list_carries_the_order_sheet_data(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    """The admin order sheet prints product codes, names, VAT and the address."""
    normal_v, _ = catalog
    placed = (await _checkout(client, customer_user, normal_v, qty=2)).json()

    listed = (await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))).json()
    order = next(o for o in listed if o["id"] == placed["id"])

    assert order["customer_email"] == customer_user.email
    assert order["customer_phone"] == customer_user.phone
    assert order["shipping_method"] == "CARGO"
    assert order["shipping_address"]["city"] == "Ankara"
    assert order["paid_at"] is None and order["cancelled_at"] is None
    line = order["items"][0]
    assert line["sku"] == normal_v.sku
    assert line["product_name"] == "Normal Nargile"
    assert line["product_code"].startswith("NRM-")
    assert Decimal(line["vat_rate"]) == Decimal("18.00")

    approved = (await _approve(client, admin_user, placed["id"])).json()
    assert approved["paid_at"] is not None


async def test_admin_cancel_restores_stock_and_points(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession, catalog
):
    normal_v, _ = catalog
    variant_id, user_id = normal_v.id, customer_user.id
    db_session.add(LoyaltyLedger(
        user_id=customer_user.id, amount=300, transaction_type=TransactionType.EARNED,
        source=PointSource.ADMIN, expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    await db_session.commit()
    stock_before = await db_session.scalar(select(Inventory.quantity).where(Inventory.variant_id == normal_v.id))

    order = (await _checkout(client, customer_user, normal_v, qty=2, points_to_redeem=300)).json()
    assert (await get_wallet(db_session, customer_user.id)).balance == 0

    bad = await client.post(f"/api/v1/admin/orders/{order['id']}/cancel", headers=auth_header(admin_user), json={"reason": ""})
    assert bad.status_code == 422
    resp = await client.post(
        f"/api/v1/admin/orders/{order['id']}/cancel",
        headers=auth_header(admin_user),
        json={"reason": "Havale gelmedi"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "CANCELLED" and resp.json()["cancel_reason"] == "Havale gelmedi"
    # Same shape as the list, so the admin's open order sheet can replace the row.
    assert resp.json()["items"][0]["product_name"] == "Normal Nargile"

    # Column queries read straight from the database (no stale identity-map values).
    stock_after = await db_session.scalar(select(Inventory.quantity).where(Inventory.variant_id == variant_id))
    assert stock_after == stock_before
    assert (await get_wallet(db_session, user_id)).balance == 300  # points came back
    # Cancelled orders can't be approved or cancelled again.
    assert (await _approve(client, admin_user, order["id"])).status_code == 409


async def test_bank_accounts_public_list_and_admin_crud(client: AsyncClient, admin_user: User, db_session: AsyncSession):
    h = auth_header(admin_user)
    created = await client.post(
        "/api/v1/admin/bank-accounts", headers=h,
        json={"bank_name": "Test Bankası", "account_holder": "Nargile Malzemesi Ltd.", "iban": VALID_IBAN},
    )
    assert created.status_code == 201, created.text
    account = created.json()
    assert account["iban"] == "TR330006100519786457841326"
    assert (await client.post("/api/v1/admin/bank-accounts", headers=h, json={
        "bank_name": "Başka Banka", "account_holder": "Dup", "iban": VALID_IBAN,
    })).status_code == 409
    assert any(a["id"] == account["id"] for a in (await client.get("/api/v1/bank-accounts")).json())

    hidden = await client.put(f"/api/v1/admin/bank-accounts/{account['id']}", headers=h, json={**account, "is_active": False})
    assert hidden.status_code == 200
    assert all(a["id"] != account["id"] for a in (await client.get("/api/v1/bank-accounts")).json())
    assert (await client.delete(f"/api/v1/admin/bank-accounts/{account['id']}", headers=h)).status_code == 204


@pytest.mark.parametrize("iban", ["TR33 0006 1005 1978 6457 8413 27", "DE89370400440532013000", "TR12"])
def test_iban_validation_rejects_bad_ibans(iban: str):
    with pytest.raises(ValueError):
        normalise_tr_iban(iban)


# ── Istanbul courier ─────────────────────────────────────────────────────────


async def test_courier_quote_is_free_and_labelled(client: AsyncClient, customer_user: User, catalog):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": str(normal_v.id), "quantity": 1}],
            "shipping_method": "COURIER_ISTANBUL",
            "shipping_address": ISTANBUL_ADDRESS,
        },
    )
    data = resp.json()
    assert Decimal(data["cargo_cost"]) == Decimal("0.00")
    assert data["shipping"]["reason"] == "courier"


async def test_courier_only_for_istanbul(client: AsyncClient, customer_user: User, catalog):
    normal_v, _ = catalog
    resp = await _checkout(client, customer_user, normal_v, shipping_method="COURIER_ISTANBUL")
    assert resp.status_code == 422
    ok = await _checkout(client, customer_user, normal_v, shipping_method="COURIER_ISTANBUL", shipping_address=ISTANBUL_ADDRESS)
    assert ok.status_code == 201, ok.text
    assert ok.json()["shipping_method"] == "COURIER_ISTANBUL"
    assert Decimal(ok.json()["shipping_cost"]) == Decimal("0.00")
    assert ok.json()["shipping_address"]["city"] == "İstanbul"  # canonical spelling


async def test_admin_filters_courier_orders(client: AsyncClient, admin_user: User, customer_user: User, catalog):
    normal_v, _ = catalog
    courier = (await _checkout(client, customer_user, normal_v, shipping_method="COURIER_ISTANBUL", shipping_address=ISTANBUL_ADDRESS)).json()
    cargo = (await _checkout(client, customer_user, normal_v)).json()
    rows = (await client.get(
        "/api/v1/admin/orders", headers=auth_header(admin_user), params={"shipping_method": "COURIER_ISTANBUL", "limit": 200}
    )).json()
    ids = {r["id"] for r in rows}
    assert courier["id"] in ids and cargo["id"] not in ids


async def test_last_address_prefill(client: AsyncClient, customer_user: User, catalog):
    normal_v, _ = catalog
    assert (await client.get("/api/v1/me/last-address", headers=auth_header(customer_user))).json() is None
    await _checkout(client, customer_user, normal_v, shipping_address=ISTANBUL_ADDRESS)
    last = (await client.get("/api/v1/me/last-address", headers=auth_header(customer_user))).json()
    assert last["district"] == "Kadıköy"


# ── ParaPuan & Kumbarapuan ───────────────────────────────────────────────────


async def test_parapuan_on_paid_order_in_band(client: AsyncClient, admin_user: User, customer_user: User, catalog):
    normal_v, _ = catalog
    order = (await _checkout(client, customer_user, normal_v, qty=5)).json()  # 25,000
    resp = await _approve(client, admin_user, order["id"])
    assert resp.json()["loyalty_points_awarded"] == 2500
    wallet = (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()
    assert wallet["balance"] == 2500
    assert wallet["history"][0]["source"] == "PARAPUAN"


async def test_kumbara_grants_5pct_when_small_orders_reach_20k(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    """4 × ₺5,000 paid orders: nothing until the 4th brings the total to ₺20,000 → 1,000 points."""
    normal_v, _ = catalog
    awards = []
    for _ in range(4):
        order = (await _checkout(client, customer_user, normal_v, qty=1)).json()
        awards.append((await _approve(client, admin_user, order["id"])).json()["loyalty_points_awarded"])
    assert awards == [0, 0, 0, 1000]

    wallet = (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()
    assert wallet["balance"] == 1000
    assert Decimal(wallet["kumbara"]["accumulated"]) == 0  # counter restarted
    assert wallet["history"][0]["source"] == "KUMBARA"

    # The next small order starts a fresh accumulation.
    order = (await _checkout(client, customer_user, normal_v, qty=1)).json()
    await _approve(client, admin_user, order["id"])
    wallet = (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()
    assert Decimal(wallet["kumbara"]["accumulated"]) == Decimal("5000.00")


async def test_kumbara_ignores_unpaid_and_large_orders(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    await _checkout(client, customer_user, normal_v, qty=3)  # 15,000 but never paid
    big = (await _checkout(client, customer_user, normal_v, qty=5)).json()  # 25,000: ParaPuan, not kumbara
    await _approve(client, admin_user, big["id"])
    wallet = (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()
    assert Decimal(wallet["kumbara"]["accumulated"]) == 0


async def test_points_expire_after_a_year_and_spend_fifo(db_session: AsyncSession, customer_user: User):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    for amount, at, exp in [(100, old, old + timedelta(days=365)), (200, now - timedelta(days=10), now + timedelta(days=355))]:
        db_session.add(LoyaltyLedger(user_id=customer_user.id, amount=amount, transaction_type=TransactionType.EARNED,
                                     source=PointSource.PARAPUAN, created_at=at, expires_at=exp))
    # Spent 80 while the old lot was still valid: comes out of the soonest-expiring lot.
    db_session.add(LoyaltyLedger(user_id=customer_user.id, amount=80, transaction_type=TransactionType.SPENT,
                                 source=PointSource.CHECKOUT, created_at=old + timedelta(days=1)))
    await db_session.commit()
    wallet = await get_wallet(db_session, customer_user.id)
    assert wallet.balance == 200  # old lot (20 left) expired; the new 200 untouched


# ── Admin wallet management ──────────────────────────────────────────────────


async def test_admin_adjusts_customer_points(client: AsyncClient, admin_user: User, customer_user: User):
    h = auth_header(admin_user)
    url = f"/api/v1/admin/customers/{customer_user.id}/points"
    added = await client.post(url, headers=h, json={"amount": 500, "note": "Hoş geldin hediyesi"})
    assert added.status_code == 200, added.text
    assert added.json()["balance"] == 500
    assert (await client.post(url, headers=h, json={"amount": -600, "note": "Fazla"})).status_code == 422
    removed = await client.post(url, headers=h, json={"amount": -200, "note": "Düzeltme"})
    assert removed.json()["balance"] == 300
    assert (await client.post(url, headers=h, json={"amount": 0, "note": "Sıfır"})).status_code == 422
    assert (await client.post(url, headers=h, json={"amount": 10, "note": ""})).status_code == 422

    listed = (await client.get("/api/v1/admin/customers", headers=h, params={"q": customer_user.email})).json()
    assert listed[0]["balance"] == 300
    wallet = (await client.get(f"/api/v1/admin/customers/{customer_user.id}/wallet", headers=h)).json()
    assert [e["points"] for e in wallet["history"][:2]] == [-200, 500]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/customers"),
        ("GET", f"/api/v1/admin/customers/{uuid.uuid4()}/wallet"),
        ("POST", f"/api/v1/admin/customers/{uuid.uuid4()}/points"),
        ("GET", "/api/v1/admin/bank-accounts"),
        ("POST", "/api/v1/admin/bank-accounts"),
        ("POST", f"/api/v1/admin/orders/{uuid.uuid4()}/cancel"),
    ],
)
async def test_admin_operations_require_admin(client: AsyncClient, customer_user: User, method: str, path: str):
    assert (await client.request(method, path, headers=auth_header(customer_user), json={})).status_code == 403


async def test_wallet_requires_login(client: AsyncClient):
    assert (await client.get("/api/v1/me/wallet")).status_code == 401


# ── Karşı ödemeli shipping: no fee on the order, consent required ────────────


async def test_receiver_pays_order_charges_no_carriage_and_needs_consent(
    client: AsyncClient, customer_user: User, db_session: AsyncSession, segments
):
    """Below the free-shipping threshold nothing is charged, but the buyer has to
    accept paying the carrier on delivery."""
    cheap = await _seed_cheap_variant(db_session, segments[SegmentType.RETAIL].id)

    body = {
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(cheap.id), "quantity": 1}],
        "shipping_address": ADDRESS,
    }
    refused = await client.post("/api/v1/orders/checkout", headers=auth_header(customer_user), json=body)
    assert refused.status_code == 422
    assert "alıcı tarafından" in refused.json()["detail"]

    placed = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(customer_user), json={**body, "shipping_consent": True}
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert Decimal(order["shipping_cost"]) == Decimal("0.00")
    assert order["shipping_payer"] == "RECEIVER"
    assert order["shipping_consent_at"] is not None


async def test_free_shipping_order_is_paid_by_the_shop(client: AsyncClient, customer_user: User, catalog):
    """A cart over the threshold needs no consent: the shop pays the carrier."""
    normal_v, _ = catalog
    body = {
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(normal_v.id), "quantity": 1}],
        "shipping_address": ADDRESS,
    }
    placed = await client.post("/api/v1/orders/checkout", headers=auth_header(customer_user), json=body)
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_payer"] == "SHOP"
    assert placed.json()["shipping_consent_at"] is None


# ── Dispatch: "Kargoya ver" + the customer's e-mail ──────────────────────────


async def test_admin_ships_approved_order_and_notifies_the_customer(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, caplog
):
    normal_v, _ = catalog
    order = (await _checkout(client, customer_user, normal_v)).json()
    url = f"/api/v1/admin/orders/{order['id']}/ship"
    h = auth_header(admin_user)

    # Only a paid order can go to the carrier.
    assert (await client.post(url, headers=h, json={})).status_code == 409

    await _approve(client, admin_user, order["id"])
    with caplog.at_level(logging.INFO, logger="app.services.mailer"):
        shipped = await client.post(url, headers=h, json={"tracking_code": "1234567890"})
    assert shipped.status_code == 200, shipped.text
    assert (shipped.json()["status"], shipped.json()["tracking_code"]) == ("SHIPPED", "1234567890")

    # No SMTP server in tests: the notice is logged instead of sent.
    mail = "\n".join(r.getMessage() for r in caplog.records)
    assert "kargoya verildi" in mail and order["order_number"] in mail
    assert "Normal Nargile" in mail  # the dispatched line, by name

    # A shipped order cannot be shipped or cancelled again.
    assert (await client.post(url, headers=h, json={})).status_code == 409
    assert (
        await client.post(
            f"/api/v1/admin/orders/{order['id']}/cancel", headers=h, json={"reason": "Çok geç"}
        )
    ).status_code == 409


async def test_admin_rejects_approved_order_and_voids_earned_points(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession, catalog
):
    """"Reddet" on a paid order: stock returns and the ParaPuan it earned is void."""
    normal_v, _ = catalog
    user_id = customer_user.id
    order = (await _checkout(client, customer_user, normal_v, qty=5)).json()  # 25,000 → 10% ParaPuan
    stock_after_order = await db_session.scalar(
        select(Inventory.quantity).where(Inventory.variant_id == normal_v.id)
    )

    await _approve(client, admin_user, order["id"])
    assert (await get_wallet(db_session, user_id)).balance == 2500

    rejected = await client.post(
        f"/api/v1/admin/orders/{order['id']}/cancel",
        headers=auth_header(admin_user),
        json={"reason": "Stok hasarlı, gönderilemedi"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "CANCELLED"
    assert (await get_wallet(db_session, user_id)).balance == 0
    assert await db_session.scalar(
        select(Inventory.quantity).where(Inventory.variant_id == normal_v.id)
    ) == stock_after_order + 5
