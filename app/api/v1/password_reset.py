""""Şifremi unuttum": request a reset by e-mail or SMS, then set a new password.

The request always answers the same, whether or not the account exists, so the
form cannot be used to find out who has one.
"""

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import ADMIN_PASSWORD_MIN
from app.schemas.communication import PasswordResetCheck, PasswordResetConfirm, PasswordResetRequest
from app.services import password_reset

router = APIRouter(prefix="/auth/password-reset", tags=["auth"])

SENT_NOTICE = "Bu bilgilerle kayıtlı bir hesap varsa şifre yenileme bilgileri gönderildi."

# Requests per client address per window, on top of the per-account limit.
# In-process: one per worker, which is enough to blunt a script.
IP_LIMIT, IP_WINDOW = 10, 15 * 60
_hits: dict[str, deque[float]] = defaultdict(deque)


def _throttle(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    hits = _hits[key]
    while hits and now - hits[0] > IP_WINDOW:
        hits.popleft()
    if len(hits) >= IP_LIMIT:
        raise HTTPException(status_code=429, detail="Çok fazla deneme. Lütfen biraz sonra tekrar deneyin.")
    hits.append(now)


@router.post("/request", status_code=status.HTTP_202_ACCEPTED)
async def request_reset(payload: PasswordResetRequest, request: Request, db: AsyncSession = Depends(get_db)):
    _throttle(request)
    await password_reset.request_reset(db, payload.channel, payload.identifier)
    return {"detail": SENT_NOTICE}


@router.post("/check")
async def check_link(payload: PasswordResetCheck, db: AsyncSession = Depends(get_db)):
    """Whether an e-mailed link can still be used, so the page can say so before the form."""
    return {"valid": await password_reset.link_token_row(db, payload.token) is not None}


@router.post("/confirm")
async def confirm_reset(payload: PasswordResetConfirm, request: Request, db: AsyncSession = Depends(get_db)):
    if payload.code:
        _throttle(request)  # six digits: guessing them is what the limit is for
    try:
        done = await password_reset.reset_password(
            db, new_password=payload.new_password, token=payload.token, phone=payload.phone, code=payload.code
        )
    except password_reset.PasswordTooShort:
        raise HTTPException(
            status_code=422,
            detail=f"Yönetici hesaplarında şifre en az {ADMIN_PASSWORD_MIN} karakter olmalı.",
        ) from None
    if not done:
        raise HTTPException(status_code=400, detail="Bağlantı ya da kod geçersiz, kullanılmış veya süresi dolmuş.")
    return {"detail": "Şifreniz değiştirildi. Yeni şifrenizle giriş yapabilirsiniz."}
