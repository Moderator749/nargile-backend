"""Ekip Kanalı: the panel team's shared channel, and Günün Notu above it.

Admins and staff alike. The panel polls — no socket: the drawer asks for
`?after_id=<the last it has>` every few seconds while it is open, and only for
the unread count while it is closed — so a message is a plain row, a cursor
is its id, and a deploy's restart loses nothing.

Messages are stored as typed. The panel finds order and product references in
them when it shows them; nothing here renders or trusts markup.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_team_user
from app.core.database import get_db
from app.models.admin_chat import AdminMessage, AdminMessageRead
from app.models.user import User, UserRole
from app.schemas.admin_chat import (
    DailyNoteRead,
    DailyNoteWrite,
    MarkRead,
    TeamMessageCreate,
    TeamMessagePage,
    TeamMessageRead,
    UnreadRead,
)
from app.services.shop_settings import read_setting, save_setting

router = APIRouter(prefix="/admin/team", tags=["admin-team"])

PAGE = 50
PAGE_MAX = 100
# How long a withdrawn message is reported, so a drawer left open drops it.
# Longer than any poll interval by far; a drawer opened later never had it.
DELETED_WINDOW = timedelta(minutes=10)
# At most this many messages from one member in this long.
RATE_MAX = 10
RATE_WINDOW = timedelta(seconds=30)
DAILY_NOTE_KEY = "admin_daily_note"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def display_name(user: User) -> str:
    """How a member is named to the others, as the order panel names them."""
    return (user.username or user.full_name or user.email)[:150]


def _read(message: AdminMessage, me: User) -> TeamMessageRead:
    mine = message.sender_id == me.id
    return TeamMessageRead(
        id=message.id,
        sender_id=message.sender_id,
        sender_name=message.sender_name,
        body=message.body,
        created_at=message.created_at,
        mine=mine,
        can_delete=mine or me.role == UserRole.SUPER_ADMIN,
    )


async def _mark_read(db: AsyncSession, user: User, upto: int) -> AdminMessageRead:
    """Move the member's marker forward to `upto`; never back."""
    row = await db.get(AdminMessageRead, user.id)
    if row is None:
        row = AdminMessageRead(user_id=user.id, last_read_id=0)
        db.add(row)
    row.last_read_id = max(row.last_read_id or 0, upto)
    row.updated_at = _now()
    return row


async def _unread(db: AsyncSession, me: User) -> UnreadRead:
    last_read = await db.scalar(select(AdminMessageRead.last_read_id).where(AdminMessageRead.user_id == me.id)) or 0
    live = AdminMessage.deleted_at.is_(None)
    latest = await db.scalar(select(func.max(AdminMessage.id)).where(live)) or 0
    # Someone else's, after the marker: one's own message is never news.
    count = await db.scalar(
        select(func.count()).where(
            live,
            AdminMessage.id > last_read,
            (AdminMessage.sender_id != me.id) | AdminMessage.sender_id.is_(None),
        )
    )
    return UnreadRead(count=count or 0, latest_id=latest, last_read_id=last_read)


# ── Messages ─────────────────────────────────────────────────────────────────


@router.get("/messages", response_model=TeamMessagePage)
async def list_messages(
    after_id: int | None = Query(None, ge=0, description="Only the newer ones: the drawer's poll."),
    before_id: int | None = Query(None, ge=1, description="The older ones: 'load earlier'."),
    limit: int = Query(PAGE, ge=1, le=PAGE_MAX),
    me: User = Depends(get_current_team_user),
    db: AsyncSession = Depends(get_db),
):
    """Oldest first, always. With neither cursor, the latest page; with
    `after_id`, what came since (`has_more`: more than a page came, ask again);
    with `before_id`, the page before it (`has_more`: still older ones)."""
    if after_id is not None and before_id is not None:
        raise HTTPException(status_code=422, detail="after_id ve before_id birlikte kullanılamaz")
    live = select(AdminMessage).where(AdminMessage.deleted_at.is_(None))
    if after_id is not None:
        rows = list(
            (await db.scalars(live.where(AdminMessage.id > after_id).order_by(AdminMessage.id).limit(limit + 1))).all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        q = live.where(AdminMessage.id < before_id) if before_id is not None else live
        rows = list((await db.scalars(q.order_by(AdminMessage.id.desc()).limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit][::-1]
    deleted = (
        await db.scalars(
            select(AdminMessage.id).where(AdminMessage.deleted_at >= _now() - DELETED_WINDOW).order_by(AdminMessage.id)
        )
    ).all()
    return TeamMessagePage(messages=[_read(m, me) for m in rows], deleted_ids=list(deleted), has_more=has_more)


@router.post("/messages", response_model=TeamMessageRead, status_code=status.HTTP_201_CREATED)
async def post_message(
    payload: TeamMessageCreate,
    me: User = Depends(get_current_team_user),
    db: AsyncSession = Depends(get_db),
):
    """Post to the channel. One's own message counts as read."""
    recent = await db.scalar(
        select(func.count()).where(AdminMessage.sender_id == me.id, AdminMessage.created_at >= _now() - RATE_WINDOW)
    )
    if (recent or 0) >= RATE_MAX:
        raise HTTPException(status_code=429, detail="Çok hızlı mesaj gönderiyorsunuz; birkaç saniye sonra yeniden deneyin.")
    message = AdminMessage(sender_id=me.id, sender_name=display_name(me), body=payload.body, created_at=_now())
    db.add(message)
    await db.flush()
    await _mark_read(db, me, message.id)
    await db.commit()
    return _read(message, me)


@router.delete("/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: int,
    me: User = Depends(get_current_team_user),
    db: AsyncSession = Depends(get_db),
):
    """Withdraw a message: one's own, or any for a super admin. The row stays,
    with who withdrew it; nobody is shown it again."""
    message = await db.get(AdminMessage, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Mesaj bulunamadı")
    if message.sender_id != me.id and me.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Yalnızca kendi mesajınızı silebilirsiniz")
    message.deleted_at = _now()
    message.deleted_by = me.id
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Unread ───────────────────────────────────────────────────────────────────


@router.get("/unread", response_model=UnreadRead)
async def unread(me: User = Depends(get_current_team_user), db: AsyncSession = Depends(get_db)):
    """The badge: how many of the others' messages this member has not seen."""
    return await _unread(db, me)


@router.post("/read", response_model=UnreadRead)
async def mark_read(payload: MarkRead, me: User = Depends(get_current_team_user), db: AsyncSession = Depends(get_db)):
    """Seen up to `last_read_id` (held to the newest message there is)."""
    newest = await db.scalar(select(func.max(AdminMessage.id))) or 0
    await _mark_read(db, me, min(payload.last_read_id, newest))
    await db.commit()
    return await _unread(db, me)


# ── Günün Notu ───────────────────────────────────────────────────────────────


def _note(raw: str | None) -> DailyNoteRead:
    if not raw:
        return DailyNoteRead(text=None, updated_by_name=None, updated_at=None)
    try:
        data = json.loads(raw)
        return DailyNoteRead(
            text=data.get("text") or None,
            updated_by_name=data.get("updated_by_name"),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
        )
    except (ValueError, TypeError, KeyError):
        # Unreadable: no note rather than a broken channel.
        return DailyNoteRead(text=None, updated_by_name=None, updated_at=None)


@router.get("/daily-note", response_model=DailyNoteRead)
async def get_daily_note(_me: User = Depends(get_current_team_user), db: AsyncSession = Depends(get_db)):
    return _note(await read_setting(db, DAILY_NOTE_KEY))


@router.put("/daily-note", response_model=DailyNoteRead)
async def put_daily_note(
    payload: DailyNoteWrite,
    me: User = Depends(get_current_team_user),
    db: AsyncSession = Depends(get_db),
):
    """Set Günün Notu — super admins only; an empty text clears it (and who
    cleared it, when, is kept)."""
    if me.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Günün Notu'nu yalnızca süper yönetici değiştirebilir")
    value = json.dumps(
        {"text": payload.text or None, "updated_by_name": display_name(me), "updated_at": _now().isoformat()},
        ensure_ascii=False,
    )
    await save_setting(db, DAILY_NOTE_KEY, value, admin_id=me.id)
    await db.commit()
    return _note(value)
