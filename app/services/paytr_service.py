"""PayTR iFrame API: the token that opens the payment page, and the
notification that says how it went.

Both directions are signed with HMAC-SHA256 under the merchant key, salted with
the merchant salt, and base64-encoded — per PayTR's integration guide:

  token request   merchant_id + user_ip + merchant_oid + email + payment_amount
                  + user_basket + no_installment + max_installment + currency
                  + test_mode, then + merchant_salt
  notification    merchant_oid + merchant_salt + status + total_amount

Amounts go to PayTR in kuruş (₺12,34 → 1234), and the basket is a base64 JSON
list of [name, unit price, quantity]. Nothing here touches the database; the
endpoints in ``app.api.v1.payments`` do.
"""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import httpx

TOKEN_URL = "https://www.paytr.com/odeme/api/get-token"
IFRAME_URL = "https://www.paytr.com/odeme/guvenli/{token}"
CURRENCY = "TL"


class PaytrError(Exception):
    """PayTR refused the token request, or could not be reached."""


@dataclass(frozen=True)
class PaytrCredentials:
    merchant_id: str
    merchant_key: str
    merchant_salt: str


def _sign(message: str, creds: PaytrCredentials) -> str:
    digest = hmac.new(creds.merchant_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def kurus(amount: Decimal) -> int:
    """₺12,345 → 1235: what PayTR calls payment_amount."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def merchant_oid(order_number: str, attempt: int) -> str:
    """PayTR takes letters and digits only, up to 64: "ORD-20261013-AB12" and
    attempt 2 → "ORD20261013AB12P2". One per attempt, so a retried payment page
    is a new reference."""
    return f"{re.sub(r'[^A-Za-z0-9]', '', order_number)}P{attempt}"[:64]


def basket(lines: list[tuple[str, Decimal, int]]) -> str:
    """[(name, unit price incl. VAT, quantity)] as PayTR's user_basket."""
    items = [[name[:100], f"{price:.2f}", quantity] for name, price, quantity in lines]
    return base64.b64encode(json.dumps(items, ensure_ascii=False).encode()).decode()


def token_hash(
    creds: PaytrCredentials,
    *,
    user_ip: str,
    merchant_oid: str,
    email: str,
    payment_amount: int,
    user_basket: str,
    no_installment: int,
    max_installment: int,
    currency: str,
    test_mode: int,
) -> str:
    message = (
        f"{creds.merchant_id}{user_ip}{merchant_oid}{email}{payment_amount}{user_basket}"
        f"{no_installment}{max_installment}{currency}{test_mode}"
    )
    return _sign(message + creds.merchant_salt, creds)


def notification_hash(creds: PaytrCredentials, *, merchant_oid: str, status: str, total_amount: str) -> str:
    return _sign(f"{merchant_oid}{creds.merchant_salt}{status}{total_amount}", creds)


def verify_notification(creds: PaytrCredentials, form: dict[str, str]) -> bool:
    """Whether a notification really comes from PayTR (constant-time compare)."""
    expected = notification_hash(
        creds,
        merchant_oid=form.get("merchant_oid", ""),
        status=form.get("status", ""),
        total_amount=form.get("total_amount", ""),
    )
    return hmac.compare_digest(expected, form.get("hash", ""))


@dataclass(frozen=True)
class TokenRequest:
    user_ip: str
    merchant_oid: str
    email: str
    amount: Decimal
    lines: list[tuple[str, Decimal, int]]
    user_name: str
    user_address: str
    user_phone: str
    ok_url: str
    fail_url: str
    test_mode: bool
    timeout_minutes: int
    no_installment: int = 0
    max_installment: int = 0


def token_form(creds: PaytrCredentials, req: TokenRequest) -> dict[str, str]:
    """The form PayTR's get-token endpoint expects, signed."""
    payment_amount = kurus(req.amount)
    user_basket = basket(req.lines)
    test_mode = 1 if req.test_mode else 0
    return {
        "merchant_id": creds.merchant_id,
        "user_ip": req.user_ip,
        "merchant_oid": req.merchant_oid,
        "email": req.email,
        "payment_amount": str(payment_amount),
        "paytr_token": token_hash(
            creds,
            user_ip=req.user_ip,
            merchant_oid=req.merchant_oid,
            email=req.email,
            payment_amount=payment_amount,
            user_basket=user_basket,
            no_installment=req.no_installment,
            max_installment=req.max_installment,
            currency=CURRENCY,
            test_mode=test_mode,
        ),
        "user_basket": user_basket,
        "debug_on": "1" if req.test_mode else "0",
        "no_installment": str(req.no_installment),
        "max_installment": str(req.max_installment),
        "user_name": req.user_name[:60],
        "user_address": req.user_address[:400],
        "user_phone": req.user_phone[:20],
        "merchant_ok_url": req.ok_url,
        "merchant_fail_url": req.fail_url,
        "timeout_limit": str(req.timeout_minutes),
        "currency": CURRENCY,
        "test_mode": str(test_mode),
        "lang": "tr",
    }


async def request_token(creds: PaytrCredentials, req: TokenRequest) -> str:
    """Ask PayTR for an iframe token; PaytrError with PayTR's reason if refused."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, data=token_form(creds, req))
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PaytrError(f"PayTR'a ulaşılamadı: {exc}") from exc
    if body.get("status") != "success" or not body.get("token"):
        raise PaytrError(str(body.get("reason") or "PayTR ödeme sayfasını açmadı"))
    return body["token"]
