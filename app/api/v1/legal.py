"""The shop's legal texts: public read, admin write.

Checkout shows the live text and records that it was accepted, so these are
read on the storefront without a login. A document that has never been edited
has no row, and falls back to the text it was seeded with — the cart is never
left with an empty agreement to tick.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.core.legal_texts import DEFAULT_LEGAL
from app.models.legal import LegalDocument, LegalKey
from app.models.user import User
from app.schemas.legal import LegalDocumentRead, LegalDocumentUpdate

router = APIRouter(tags=["legal"])


async def legal_documents(db: AsyncSession) -> list[LegalDocumentRead]:
    """Every document, in panel order, each one either as edited or as seeded."""
    rows = {row.key: row for row in (await db.execute(select(LegalDocument))).scalars()}
    out: list[LegalDocumentRead] = []
    for key, (title, body) in DEFAULT_LEGAL.items():
        row = rows.get(key)
        if row is None:
            out.append(LegalDocumentRead(key=LegalKey(key), title=title, body=body))
            continue
        out.append(
            LegalDocumentRead(
                key=LegalKey(key),
                title=row.title,
                body=row.body,
                updated_at=row.updated_at,
                # The seeded rows carry the timestamp of the migration that
                # wrote them, so "edited" is the text itself having changed.
                edited=(row.title, row.body) != (title, body),
            )
        )
    return out


@router.get("/settings/legal", response_model=list[LegalDocumentRead])
async def get_legal_documents(db: AsyncSession = Depends(get_db)):
    return await legal_documents(db)


@router.get("/admin/settings/legal", response_model=list[LegalDocumentRead])
async def admin_get_legal_documents(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await legal_documents(db)


@router.put("/admin/settings/legal/{key}", response_model=LegalDocumentRead)
async def admin_update_legal_document(
    key: LegalKey,
    payload: LegalDocumentUpdate,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if key.value not in DEFAULT_LEGAL:  # pragma: no cover — the enum is the list
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Böyle bir metin yok")
    row = await db.get(LegalDocument, key.value)
    if row is None:
        row = LegalDocument(key=key.value)
        db.add(row)
    row.title = payload.title
    row.body = payload.body
    row.updated_by = admin.id
    await db.commit()
    await db.refresh(row)
    return LegalDocumentRead(
        key=key,
        title=row.title,
        body=row.body,
        updated_at=row.updated_at,
        edited=(row.title, row.body) != DEFAULT_LEGAL[key.value],
    )
