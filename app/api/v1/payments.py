"""Card payments through PayTR's iFrame API.

The order comes first: checkout creates it as AWAITING_PAYMENT, with its stock
and any Sultan Puan spent on it held, and priced by the checkout engine — so
every discount, shipping rule and point redemption is already in the total the
customer is asked to pay. Then:

1. ``POST /payments/paytr/token`` opens a payment page for that order: it signs
   the request, asks PayTR for a token, and hands back the iframe address.
2. PayTR posts the outcome to ``POST /payments/paytr/callback`` (the
   "Bildirim URL" set in the PayTR panel). That notification — signed, and
   checked here — is the only thing that marks an order paid; the page the
   customer is sent back to only shows what the notification decided.

A payment that fails releases the order (PAYMENT_FAILED); one that is never
completed is released by the hourly job once its page has long expired.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import uuid

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.catalog import Product, ProductVariant
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.payment import PaymentStatus, PaymentTransaction
from app.models.user import User
from app.services import order_lifecycle
from app.services.paytr_service import (
    IFRAME_URL,
    PaytrCredentials,
    PaytrError,
    TokenRequest,
    kurus,
    merchant_oid,
    request_token,
    verify_notification,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])

CARD_OFF = "Kart ile ödeme şu anda kullanılamıyor; Havale / EFT ile devam edebilirsiniz."


def credentials() -> PaytrCredentials:
    return PaytrCredentials(settings.PAYTR_MERCHANT_ID, settings.PAYTR_MERCHANT_KEY, settings.PAYTR_MERCHANT_SALT)


def client_ip(request: Request) -> str:
    """The customer's address, which PayTR checks: the first hop a proxy saw."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or request.headers.get("x-real-ip", "") or (request.client.host if request.client else "")


def amount_due(order: Order) -> Decimal:
    """What the customer pays: the subtotal, less every discount, plus carriage."""
    return (order.total_amount - order.discount_amount + order.shipping_cost).quantize(Decimal("0.01"))


class PaytrStatusRead(BaseModel):
    enabled: bool
    test_mode: bool


class PaytrTokenRequest(BaseModel):
    order_id: uuid.UUID


class PaytrTokenRead(BaseModel):
    token: str
    iframe_url: str
    merchant_oid: str
    amount: Decimal
    test_mode: bool


@router.get("/paytr/status", response_model=PaytrStatusRead)
async def paytr_status():
    """Whether card payment is open, so checkout can offer it or say why not."""
    return PaytrStatusRead(enabled=settings.paytr_configured, test_mode=settings.PAYTR_TEST_MODE)


@router.post("/paytr/token", response_model=PaytrTokenRead)
async def paytr_token(
    payload: PaytrTokenRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Open a PayTR payment page for one of the caller's unpaid card orders."""
    if not settings.paytr_configured:
        raise HTTPException(status_code=503, detail=CARD_OFF)
    order = await db.scalar(
        select(Order)
        .where(Order.id == payload.order_id, Order.user_id == current_user.id)
        .options(selectinload(Order.items))
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.payment_method != PaymentMethod.CREDIT_CARD or order.status != OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(status_code=409, detail="Bu sipariş için kartla ödeme beklenmiyor")

    lines = [
        (f"{name}{f' ({color})' if color else ''}", unit_price, quantity)
        for name, color, unit_price, quantity in (
            await db.execute(
                select(Product.name, ProductVariant.color_name, OrderItem.unit_price, OrderItem.quantity)
                .join(ProductVariant, OrderItem.variant_id == ProductVariant.id)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(OrderItem.order_id == order.id)
            )
        ).all()
    ]
    attempts = await db.scalar(
        select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.order_id == order.id)
    )
    oid = merchant_oid(order.order_number, attempts + 1)
    amount = amount_due(order)
    address = order.shipping_address or {}
    req = TokenRequest(
        user_ip=client_ip(request),
        merchant_oid=oid,
        email=current_user.email,
        amount=amount,
        lines=lines,
        user_name=address.get("full_name") or current_user.email,
        user_address=" ".join(
            str(address[k]) for k in ("line", "district", "city", "country") if address.get(k)
        ) or "-",
        user_phone=address.get("phone") or "-",
        ok_url=settings.PAYTR_OK_URL.format(order_id=order.id),
        fail_url=settings.PAYTR_FAIL_URL.format(order_id=order.id),
        test_mode=settings.PAYTR_TEST_MODE,
        timeout_minutes=settings.PAYTR_TIMEOUT_MINUTES,
        no_installment=settings.PAYTR_NO_INSTALLMENT,
        max_installment=settings.PAYTR_MAX_INSTALLMENT,
    )
    try:
        token = await request_token(credentials(), req)
    except PaytrError as exc:
        # The page cannot open, so the order cannot be paid: give back what it
        # held now rather than in an hour, and let the customer choose again.
        logger.warning("PayTR token refused for %s: %s", order.order_number, exc)
        user = await db.scalar(select(User).where(User.id == order.user_id).options(selectinload(User.segment)))
        await order_lifecycle.fail_payment(db, order, user, f"Ödeme sayfası açılamadı: {exc}", datetime.now(timezone.utc))
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Ödeme sayfası açılamadı: {exc}")

    db.add(PaymentTransaction(
        order_id=order.id, merchant_oid=oid, amount=amount, test_mode=settings.PAYTR_TEST_MODE,
    ))
    await db.commit()
    return PaytrTokenRead(
        token=token, iframe_url=IFRAME_URL.format(token=token), merchant_oid=oid,
        amount=amount, test_mode=settings.PAYTR_TEST_MODE,
    )


@router.post("/paytr/callback", response_class=PlainTextResponse)
async def paytr_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """PayTR's notification. Answers exactly "OK" once it is recorded — PayTR
    keeps retrying until it gets that — and refuses anything whose hash does not
    check out, so a forged request changes nothing."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not settings.paytr_configured or not verify_notification(credentials(), form):
        logger.warning("PayTR notification with a bad hash for %s", form.get("merchant_oid"))
        raise HTTPException(status_code=400, detail="PAYTR notification failed: bad hash")

    tx = await db.scalar(select(PaymentTransaction).where(PaymentTransaction.merchant_oid == form["merchant_oid"]))
    if tx is None:
        # Signed by PayTR but not ours to match: say OK so it stops retrying.
        logger.error("PayTR notification for unknown merchant_oid %s", form["merchant_oid"])
        return "OK"
    if tx.status != PaymentStatus.PENDING:
        return "OK"  # a repeat of a notification already handled

    now = datetime.now(timezone.utc)
    success = form.get("status") == "success"
    tx.status = PaymentStatus.SUCCESS if success else PaymentStatus.FAILED
    tx.completed_at = now
    tx.callback_payload = form
    tx.payment_type = form.get("payment_type") or None
    tx.failed_reason_code = form.get("failed_reason_code") or None
    tx.failed_reason_msg = (form.get("failed_reason_msg") or "")[:300] or None
    if form.get("total_amount", "").isdigit():
        tx.total_amount = Decimal(form["total_amount"]) / 100

    order = await db.scalar(select(Order).where(Order.id == tx.order_id).options(selectinload(Order.items)))
    if success:
        collected = int(form["total_amount"]) if form.get("total_amount", "").isdigit() else 0
        if collected < kurus(tx.amount):
            # Never expected (installment interest only adds): leave it for a person.
            logger.error("PayTR collected %s kuruş for %s, expected %s", collected, tx.merchant_oid, kurus(tx.amount))
        elif order.status == OrderStatus.AWAITING_PAYMENT:
            await order_lifecycle.mark_paid(db, order, now)
        else:
            # Paid after the order was released (it timed out or was cancelled):
            # the money is in, the stock is not held. A person has to decide.
            logger.error(
                "PayTR payment %s succeeded but order %s is %s; needs manual review",
                tx.merchant_oid, order.order_number, order.status,
            )
    elif order.status == OrderStatus.AWAITING_PAYMENT:
        user = await db.scalar(select(User).where(User.id == order.user_id).options(selectinload(User.segment)))
        reason = tx.failed_reason_msg or "Ödeme tamamlanamadı"
        await order_lifecycle.fail_payment(db, order, user, f"Kart ödemesi başarısız: {reason}", now)
    await db.commit()
    return "OK"
