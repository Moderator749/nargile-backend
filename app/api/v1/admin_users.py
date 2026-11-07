"""Yöneticiler: the panel team (super admins, admins, staff), managed by the
Sistem Yöneticisi alone (User.is_superadmin).

Every route is require_superadmin's. On top of it, guard_protected keeps a
Sistem Yöneticisi account to itself and its peers. The caller cannot change
its own role or state, nor remove itself, so the shop always keeps an active
Sistem Yöneticisi: the caller. Being one is set by scripts/set_superadmin.py,
never through these routes.

Removing a member does not delete the row — orders, notes and messages still
name it — it takes the account off the panel: a deactivated customer, every
session ended.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import guard_protected, require_superadmin
from app.core.database import get_db
from app.core.security import hash_password_async
from app.models.user import User, UserRole
from app.schemas.admin_users import (
    TeamMemberCreate,
    TeamMemberRead,
    TeamMemberUpdate,
    TeamPasswordSet,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

TEAM = (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.STAFF)


def _read(user: User, me: User) -> TeamMemberRead:
    return TeamMemberRead(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        role=UserRole(user.role).value,
        is_active=user.is_active,
        is_superadmin=user.is_superadmin,
        created_at=user.created_at,
        is_me=user.id == me.id,
    )


async def _member(db: AsyncSession, user_id: uuid.UUID, me: User) -> User:
    """A member of the panel team, guarded; anyone else is not found."""
    user = await db.get(User, user_id)
    if user is None or user.role not in TEAM:
        raise HTTPException(status_code=404, detail="Ekip üyesi bulunamadı")
    guard_protected(me, user)
    return user


@router.get("", response_model=list[TeamMemberRead])
async def list_team(me: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    """The team: the Sistem Yöneticisi first, then by role and name."""
    rank = case({UserRole.SUPER_ADMIN: 0, UserRole.ADMIN: 1, UserRole.STAFF: 2}, value=User.role)
    rows = await db.scalars(
        select(User)
        .where(User.role.in_(TEAM))
        .order_by(User.is_superadmin.desc(), rank, func.lower(func.coalesce(User.full_name, User.username, User.email)))
    )
    return [_read(u, me) for u in rows.all()]


@router.post("", response_model=TeamMemberRead, status_code=status.HTTP_201_CREATED)
async def create_member(
    payload: TeamMemberCreate, me: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    """A new account straight into the team. An address or username already in
    use — a customer's included — is refused: an account is not promoted here."""
    if await db.scalar(select(User.id).where(func.lower(User.email) == payload.email, User.is_guest.is_(False))):
        raise HTTPException(status_code=409, detail="Bu e-posta adresi başka bir hesapta kayıtlı")
    if payload.username and await db.scalar(
        select(User.id).where(func.lower(User.username) == payload.username.lower())
    ):
        raise HTTPException(status_code=409, detail="Bu kullanıcı adı kullanılıyor")
    user = User(
        email=payload.email,
        username=payload.username,
        full_name=payload.full_name,
        role=UserRole(payload.role),
        hashed_password=await hash_password_async(payload.password),
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _read(user, me)


@router.patch("/{user_id}", response_model=TeamMemberRead)
async def update_member(
    user_id: uuid.UUID,
    payload: TeamMemberUpdate,
    me: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Name, role, active. One's own role and state stay as they are: changing
    them is how one locks oneself out."""
    user = await _member(db, user_id, me)
    changes = payload.model_dump(exclude_unset=True)
    role = UserRole(changes["role"]) if changes.get("role") else None
    if user.id == me.id and ((role is not None and role != user.role) or changes.get("is_active") is False):
        raise HTTPException(status_code=409, detail="Kendi rolünüzü ya da durumunuzu değiştiremezsiniz")

    if "full_name" in changes:
        user.full_name = " ".join((changes["full_name"] or "").split()) or None
    if role is not None:
        user.role = role
    if "is_active" in changes and changes["is_active"] is not None:
        if user.is_active and not changes["is_active"]:
            user.token_version = (user.token_version or 0) + 1  # its sessions end now
        user.is_active = changes["is_active"]
    await db.commit()
    await db.refresh(user)
    return _read(user, me)


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def set_member_password(
    user_id: uuid.UUID,
    payload: TeamPasswordSet,
    me: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """A new password for a member; their sessions end. One's own is changed
    under Hesabım, with the current password."""
    user = await _member(db, user_id, me)
    if user.id == me.id:
        raise HTTPException(status_code=409, detail="Kendi şifrenizi Hesabım › Şifre bölümünden değiştirin")
    user.hashed_password = await hash_password_async(payload.password)
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: uuid.UUID, me: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    """Take a member off the panel: a deactivated customer account, its sessions
    ended. The row stays for what it wrote and ordered."""
    user = await _member(db, user_id, me)
    if user.id == me.id:
        raise HTTPException(status_code=409, detail="Kendi hesabınızı panelden çıkaramazsınız")
    user.role = UserRole.CUSTOMER
    user.is_superadmin = False
    user.is_active = False
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
