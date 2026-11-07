"""The admin's "Not Defteri": personal notes, each admin seeing only their own.

A note can be locked against deletion. The lock — its icon and the right to
turn it — belongs to the admin who locked it and to the "Moderator" account,
which sees every admin's notes (read-only) to manage their locks.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.note import AdminNote
from app.models.user import User
from app.schemas.note import NoteDeleteResult, NoteIds, NoteLock, NoteRead, NoteWrite

router = APIRouter(prefix="/admin/notes", tags=["admin-notes"])

MODERATOR_USERNAME = "moderator"
LOCKED_REASON = "Not kilitli: kilidi açılmadan silinemez"


def is_moderator(user: User) -> bool:
    """The account named "Moderator" (usernames are unique whatever their case);
    only an admin reaches these routes at all."""
    return (user.username or "").lower() == MODERATOR_USERNAME


def _can_manage_lock(note: AdminNote, user: User) -> bool:
    """Lock an unlocked note it may see; unlock only a lock of its own — or any, as the Moderator."""
    if is_moderator(user):
        return True
    return note.locked_by_id == user.id if note.is_locked else note.user_id == user.id


def _read(note: AdminNote, viewer: User, author: User | None = None) -> NoteRead:
    manage = _can_manage_lock(note, viewer)
    own = note.user_id == viewer.id
    return NoteRead(
        id=note.id,
        title=note.title,
        body=note.body,
        created_at=note.created_at,
        updated_at=note.updated_at,
        own=own,
        author=None if own or author is None else (author.full_name or author.username or author.email),
        locked=note.is_locked if manage else None,
        can_manage_lock=manage,
    )


async def _own(db: AsyncSession, note_id: int, admin: User) -> AdminNote:
    """The admin's own note; another admin's is "not found", never "forbidden"."""
    note = await db.get(AdminNote, note_id)
    if note is None or note.user_id != admin.id:
        raise HTTPException(status_code=404, detail="Not bulunamadı")
    return note


@router.get("", response_model=list[NoteRead])
async def list_notes(admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """The admin's notes, the last edited first; the Moderator's list has every admin's."""
    query = select(AdminNote, User).join(User, User.id == AdminNote.user_id)
    if not is_moderator(admin):
        query = query.where(AdminNote.user_id == admin.id)
    rows = await db.execute(query.order_by(AdminNote.updated_at.desc(), AdminNote.id.desc()))
    return [_read(note, admin, author) for note, author in rows.all()]


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: NoteWrite, admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    note = AdminNote(user_id=admin.id, **payload.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return _read(note, admin)


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
    return _read(note, admin)


@router.put("/{note_id}/lock", response_model=NoteRead)
async def set_note_lock(
    note_id: int,
    payload: NoteLock,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Lock or unlock a note: its author or the Moderator locks it, and only who
    locked it, or the Moderator, unlocks it."""
    note = await db.get(AdminNote, note_id)
    if note is None or (note.user_id != admin.id and not is_moderator(admin)):
        raise HTTPException(status_code=404, detail="Not bulunamadı")
    if note.is_locked != payload.locked:
        if not _can_manage_lock(note, admin):
            raise HTTPException(status_code=403, detail="Bu kilidi yalnızca kilitleyen admin ya da Moderator açabilir")
        # The lock is not an edit: updated_at set to itself keeps the column's
        # onupdate off, and the note its place in the list.
        await db.execute(
            update(AdminNote)
            .where(AdminNote.id == note.id)
            .values(
                is_locked=payload.locked,
                locked_by_id=admin.id if payload.locked else None,
                updated_at=AdminNote.updated_at,
            )
        )
        await db.commit()
        await db.refresh(note)
    author = None if note.user_id == admin.id else await db.get(User, note.user_id)
    return _read(note, admin, author)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: int, admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    note = await _own(db, note_id, admin)
    if note.is_locked:
        raise HTTPException(status_code=409, detail=LOCKED_REASON)
    await db.delete(note)
    await db.commit()


@router.post("/delete", response_model=NoteDeleteResult)
async def delete_notes(
    payload: NoteIds, admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    """Delete the ticked notes of the admin's own; a locked one is kept and reported back."""
    result = NoteDeleteResult()
    notes = {
        n.id: n
        for n in (
            await db.scalars(
                select(AdminNote).where(AdminNote.id.in_(payload.note_ids), AdminNote.user_id == admin.id)
            )
        ).all()
    }
    for note_id in dict.fromkeys(payload.note_ids):
        note = notes.get(note_id)
        if note is None:
            result.refused[note_id] = "Not bulunamadı"
        elif note.is_locked:
            result.refused[note_id] = LOCKED_REASON
        else:
            await db.delete(note)
            result.deleted.append(note_id)
    await db.commit()
    return result
