"""The admin's "Not Defteri": personal notes, each admin seeing only their own."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.note import AdminNote
from app.models.user import User
from app.schemas.note import NoteRead, NoteWrite

router = APIRouter(prefix="/admin/notes", tags=["admin-notes"])


async def _own(db: AsyncSession, note_id: int, admin: User) -> AdminNote:
    """The admin's own note; another admin's is "not found", never "forbidden"."""
    note = await db.get(AdminNote, note_id)
    if note is None or note.user_id != admin.id:
        raise HTTPException(status_code=404, detail="Not bulunamadı")
    return note


@router.get("", response_model=list[NoteRead])
async def list_notes(admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """The admin's notes, the last edited first."""
    rows = await db.execute(
        select(AdminNote).where(AdminNote.user_id == admin.id).order_by(AdminNote.updated_at.desc(), AdminNote.id.desc())
    )
    return rows.scalars().all()


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: NoteWrite, admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    note = AdminNote(user_id=admin.id, **payload.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@router.put("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: int,
    payload: NoteWrite,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _own(db, note_id, admin)
    note.title, note.body = payload.title, payload.body
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: int, admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    await db.delete(await _own(db, note_id, admin))
    await db.commit()
