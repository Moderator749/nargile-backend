"""PayTR card payments.

Covers:
- The token and notification hashes, per PayTR's specification
- Amounts in kuruş, merchant_oid shape, the basket encoding
- The token endpoint: priced from the order, owner-only, card orders only, off
  without credentials, and a refused page releasing the order
- The notification: a signed success marks the order paid (once), a bad hash
  is refused, a failure releases stock and points, a short amount pays nothing
- An abandoned payment page released by the hourly job
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import payments
from app.core.config import settings
from app.models.catalog import Inventory, ProductVariant, WarehouseType
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.notification import NotificationLog
from app.models.order import Order
from app.models.payment import PaymentStatus, PaymentTransaction
from app.models.user import User
from app.services.order_tasks import cancel_expired_pending_orders
from app.services.paytr_service import (
    PaytrCredentials,
    PaytrError,
    TokenRequest,
    basket,
    kurus,
    merchant_oid,
    notification_hash,
    token_form,
    token_hash,
    verify_notification,
)
from tests.conftest import auth_header
from tests.test_operations import _checkout

CREDS = PaytrCredentials("100000", "test-merchant-key", "test-merchant-salt")


def _hmac(message: str) -> str:
    return base64.b64encode(hmac.new(b"test-merchant-key", message.encode(), hashlib.sha256).digest()).decode()


# ── Hashes and encodings ─────────────────────────────────────────────────────


def test_the_token_hash_follows_paytrs_formula():
    got = token_hash(
        CREDS, user_ip="85.34.78.112", merchant_oid="ORD1P1", email="a@b.com", payment_amount=10050,
        user_basket="W10=", no_installment=0, max_installment=0, currency="TL", test_mode=1,
    )
    assert got == _hmac("10000085.34.78.112ORD1P1a@b.com10050W10=00TL1" + "test-merchant-salt")
    # Pinned, so a change to the formula cannot pass by changing the test too.
    assert got == "idxQ+Vrh0ki5x1Cyhbg3jeKIttI8ds2Gk3hf5jLGsao="


def test_the_notification_hash_follows_paytrs_formula():
    got = notification_hash(CREDS, merchant_oid="ORD1P1", status="success", total_amount="10050")
    assert got == _hmac("ORD1P1" + "test-merchant-salt" + "success" + "10050")
    form = {"merchant_oid": "ORD1P1", "status": "success", "total_amount": "10050", "hash": got}
    assert verify_notification(CREDS, form)
    assert not verify_notification(CREDS, {**form, "total_amount": "10051"})
    assert not verify_notification(CREDS, {**form, "hash": "forged"})
    assert not verify_notification(PaytrCredentials("100000", "other-key", "test-merchant-salt"), form)


def test_amounts_references_and_basket():
    assert kurus(Decimal("100.50")) == 10050
    assert kurus(Decimal("0.005")) == 1  # rounded half up, as money is
    assert merchant_oid("ORD-20261013120000-AB12CD", 2) == "ORD20261013120000AB12CDP2"
    decoded = json.loads(base64.b64decode(basket([("Çelik Nargile", Decimal("4500"), 2)])))
    assert decoded == [["Çelik Nargile", "4500.00", 2]]


def test_the_token_form_signs_what_it_sends():
    form = token_form(CREDS, TokenRequest(
        user_ip="85.34.78.112", merchant_oid="ORD1P1", email="a@b.com", amount=Decimal("100.50"),
        lines=[("X", Decimal("100.50"), 1)], user_name="A B", user_address="Adres", user_phone="0532",
        ok_url="https://ok", fail_url="https://fail", test_mode=True, timeout_minutes=30,
    ))
    assert form["payment_amount"] == "10050" and form["currency"] == "TL" and form["test_mode"] == "1"
    assert form["paytr_token"] == token_hash(
        CREDS, user_ip=form["user_ip"], merchant_oid=form["merchant_oid"], email=form["email"],
        payment_amount=10050, user_basket=form["user_basket"], no_installment=0, max_installment=0,
        currency="TL", test_mode=1,
    )


# ── Token endpoint ───────────────────────────────────────────────────────────


@pytest.fixture
def fake_paytr(monkeypatch):
    """Stand in for PayTR's get-token call; records what would have been sent."""
    sent: list[TokenRequest] = []

    async def fake(creds, req):
        sent.append(req)
        return "tok123"

    monkeypatch.setattr(payments, "request_token", fake)
    return sent


async def _card_order(client: AsyncClient, user: User, variant: ProductVariant, qty: int = 1) -> dict:
    resp = await _checkout(client, user, variant, qty=qty, payment_method="CREDIT_CARD")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "AWAITING_PAYMENT"
    return resp.json()


async def _stock(db: AsyncSession, variant: ProductVariant) -> int:
    # A column, not an entity: read from the table, never from the session's cache.
    return await db.scalar(
        select(Inventory.quantity).where(
            Inventory.variant_id == variant.id, Inventory.warehouse_type == WarehouseType.RETAIL_STOCK
        )
    )


async def test_a_card_order_opens_a_payment_page_for_its_total(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog, fake_paytr
):
    normal_v, _ = catalog  # ₺5,000
    order = await _card_order(client, customer_user, normal_v, qty=2)
    resp = await client.post(
        "/api/v1/payments/paytr/token", headers={**auth_header(customer_user), "X-Forwarded-For": "85.34.78.112"},
        json={"order_id": order["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iframe_url"] == "https://www.paytr.com/odeme/guvenli/tok123"
    assert Decimal(body["amount"]) == Decimal("10000.00")

    req = fake_paytr[0]
    assert (req.user_ip, req.email, req.amount) == ("85.34.78.112", customer_user.email, Decimal("10000.00"))
    assert req.merchant_oid.isalnum() and req.merchant_oid.endswith("P1")
    assert req.lines[0][1:] == (Decimal("5000.00"), 2)
    assert order["id"] in req.ok_url and order["id"] in req.fail_url

    tx = await db_session.scalar(select(PaymentTransaction).where(PaymentTransaction.merchant_oid == req.merchant_oid))
    assert (tx.status, tx.amount) == (PaymentStatus.PENDING, Decimal("10000.00"))


async def test_only_the_owner_of_an_unpaid_card_order_can_open_its_page(
    client: AsyncClient, customer_user: User, admin_user: User, catalog, fake_paytr
):
    normal_v, _ = catalog
    order = await _card_order(client, customer_user, normal_v)
    other = await client.post("/api/v1/payments/paytr/token", headers=auth_header(admin_user), json={"order_id": order["id"]})
    assert other.status_code == 404
    transfer = (await _checkout(client, customer_user, normal_v)).json()  # havale
    refused = await client.post(
        "/api/v1/payments/paytr/token", headers=auth_header(customer_user), json={"order_id": transfer["id"]}
    )
    assert refused.status_code == 409


async def test_without_credentials_card_payment_is_off(
    client: AsyncClient, customer_user: User, catalog, monkeypatch
):
    normal_v, _ = catalog
    monkeypatch.setattr(settings, "PAYTR_MERCHANT_SALT", "")
    assert (await client.get("/api/v1/payments/paytr/status")).json()["enabled"] is False
    resp = await _checkout(client, customer_user, normal_v, payment_method="CREDIT_CARD")
    assert resp.status_code == 422 and "Havale" in resp.json()["detail"]


async def test_a_refused_payment_page_releases_the_order(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog, monkeypatch
):
    normal_v, _ = catalog
    before = await _stock(db_session, normal_v)
    order = await _card_order(client, customer_user, normal_v)
    assert await _stock(db_session, normal_v) == before - 1

    async def refuse(creds, req):
        raise PaytrError("paytr_token gonderilmedi veya gecersiz")

    monkeypatch.setattr(payments, "request_token", refuse)
    resp = await client.post("/api/v1/payments/paytr/token", headers=auth_header(customer_user), json={"order_id": order["id"]})
    assert resp.status_code == 502 and "gecersiz" in resp.json()["detail"]
    assert (await db_session.get(Order, order["id"], populate_existing=True)).status == "PAYMENT_FAILED"
    assert await _stock(db_session, normal_v) == before


# ── Notification ─────────────────────────────────────────────────────────────


async def _opened(client, user, variant, qty=1) -> tuple[dict, dict]:
    """A card order with its payment page open; (order, token response)."""
    order = await _card_order(client, user, variant, qty=qty)
    body = (await client.post(
        "/api/v1/payments/paytr/token", headers=auth_header(user), json={"order_id": order["id"]}
    )).json()
    return order, body


def _notification(oid: str, status: str, total_kurus: int, **extra) -> dict:
    total = str(total_kurus)
    return {
        "merchant_oid": oid, "status": status, "total_amount": total,
        "hash": notification_hash(CREDS, merchant_oid=oid, status=status, total_amount=total),
        "payment_type": "card", "currency": "TL", "test_mode": "1", **extra,
    }


async def test_a_signed_success_marks_the_order_paid_once(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog, fake_paytr
):
    normal_v, _ = catalog
    order, page = await _opened(client, customer_user, normal_v, qty=5)  # ₺25,000: inside the Sultan Puan band
    form = _notification(page["merchant_oid"], "success", 2500000)

    resp = await client.post("/api/v1/payments/paytr/callback", data=form)
    assert (resp.status_code, resp.text) == (200, "OK")
    paid = await db_session.get(Order, order["id"], populate_existing=True)
    assert paid.status == "APPROVED" and paid.paid_at is not None
    tx = await db_session.scalar(select(PaymentTransaction).where(PaymentTransaction.merchant_oid == page["merchant_oid"]))
    assert (tx.status, tx.total_amount, tx.payment_type) == (PaymentStatus.SUCCESS, Decimal("25000.00"), "card")

    # PayTR repeats a notification until it hears OK: the repeat changes nothing.
    again = await client.post("/api/v1/payments/paytr/callback", data=form)
    assert again.text == "OK"
    earned = (await db_session.execute(
        select(LoyaltyLedger.amount).where(
            LoyaltyLedger.order_id == paid.id, LoyaltyLedger.transaction_type == TransactionType.EARNED
        )
    )).scalars().all()
    assert earned == [2500]

    # Placed now that it is paid: "Siparişiniz alınmıştır", once, repeats or not.
    texted = (await db_session.scalars(select(NotificationLog).where(NotificationLog.purpose == "order_placed"))).all()
    assert [log.status for log in texted if (log.meta or {}).get("order_id") == order["id"]] == ["sent"]


async def test_a_bad_hash_is_refused_and_changes_nothing(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog, fake_paytr
):
    normal_v, _ = catalog
    order, page = await _opened(client, customer_user, normal_v)
    forged = {**_notification(page["merchant_oid"], "success", 500000), "hash": "Zm9yZ2Vk"}
    resp = await client.post("/api/v1/payments/paytr/callback", data=forged)
    assert resp.status_code == 400 and resp.text != "OK"
    assert (await db_session.get(Order, order["id"], populate_existing=True)).status == "AWAITING_PAYMENT"


async def test_a_failed_payment_releases_stock_and_points(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog, fake_paytr
):
    normal_v, _ = catalog
    db_session.add(LoyaltyLedger(
        user_id=customer_user.id, amount=300, transaction_type=TransactionType.ADJUSTMENT,
        source=PointSource.ADMIN, expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    await db_session.commit()
    before = await _stock(db_session, normal_v)
    order = await _card_order_with_points(client, customer_user, normal_v, points=300)
    page = (await client.post(
        "/api/v1/payments/paytr/token", headers=auth_header(customer_user), json={"order_id": order["id"]}
    )).json()
    assert (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()["balance"] == 0

    form = _notification(page["merchant_oid"], "failed", 470000, failed_reason_code="2", failed_reason_msg="Kart limiti yetersiz")
    assert (await client.post("/api/v1/payments/paytr/callback", data=form)).text == "OK"

    failed = await db_session.get(Order, order["id"], populate_existing=True)
    assert failed.status == "PAYMENT_FAILED" and "Kart limiti yetersiz" in failed.cancel_reason
    assert await _stock(db_session, normal_v) == before
    assert (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()["balance"] == 300


async def _card_order_with_points(client, user, variant, points: int) -> dict:
    resp = await _checkout(client, user, variant, payment_method="CREDIT_CARD", points_to_redeem=points)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_a_short_payment_does_not_pay_the_order(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog, fake_paytr
):
    normal_v, _ = catalog
    order, page = await _opened(client, customer_user, normal_v)  # ₺5,000 = 500000 kuruş
    form = _notification(page["merchant_oid"], "success", 100)
    assert (await client.post("/api/v1/payments/paytr/callback", data=form)).text == "OK"
    assert (await db_session.get(Order, order["id"], populate_existing=True)).status == "AWAITING_PAYMENT"


async def test_an_abandoned_payment_page_is_released_by_the_hourly_job(
    client: AsyncClient, db_session: AsyncSession, customer_user: User, catalog
):
    normal_v, _ = catalog
    before = await _stock(db_session, normal_v)
    order = await _card_order(client, customer_user, normal_v)
    await db_session.execute(
        update(Order).where(Order.id == order["id"]).values(
            created_at=datetime.now(timezone.utc) - timedelta(minutes=settings.PAYTR_ABANDON_AFTER_MINUTES + 1)
        )
    )
    await db_session.commit()
    assert await cancel_expired_pending_orders(db_session) >= 1
    assert (await db_session.get(Order, order["id"], populate_existing=True)).status == "PAYMENT_FAILED"
    assert await _stock(db_session, normal_v) == before
