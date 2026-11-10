""""Abonelikten Çık": the public, one-click way out of campaign e-mail.

POST is the unsubscribing: the mail app's button (RFC 8058 sends
`List-Unsubscribe=One-Click` as its form body) and the storefront's
/abonelikten-cik page, which posts on opening. GET only sends a browser that
opened the link itself to that page: link scanners fetch every URL in a mail,
and a GET that unsubscribed would take people off the list who never asked.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services import unsubscribe

router = APIRouter(prefix="/newsletter", tags=["newsletter"])

INVALID = "Bu abonelikten çıkma bağlantısı geçersiz. Hesabınızdaki iletişim tercihlerinden de çıkabilirsiniz."


class Unsubscribed(BaseModel):
    email: str


@router.post("/unsubscribe", response_model=Unsubscribed)
async def unsubscribe_now(request: Request, token: str = Query(..., max_length=600), db: AsyncSession = Depends(get_db)):
    email = unsubscribe.email_from(token)
    if email is None:
        raise HTTPException(status_code=400, detail=INVALID)
    one_click = b"List-Unsubscribe=One-Click" in await request.body()
    await unsubscribe.opt_out(db, email, "one_click" if one_click else "link")
    return Unsubscribed(email=email)


@router.get("/unsubscribe", status_code=303)
async def unsubscribe_page(token: str = Query(..., max_length=600)):
    # Absolute, from the setting: never a Location built off the Host this
    # was reached on (behind the proxy, that is the API's own address).
    base = settings.UNSUBSCRIBE_BASE_URL.rstrip("/")
    return RedirectResponse(f"{base}{unsubscribe.PAGE_PATH}?token={quote(token, safe='.-_')}", status_code=303)
