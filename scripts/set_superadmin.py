"""An admin made the Sistem Yöneticisi (User.is_superadmin), or no longer one.

    .venv/bin/python scripts/set_superadmin.py --username Moderator --dry-run
    .venv/bin/python scripts/set_superadmin.py --username Moderator
    .venv/bin/python scripts/set_superadmin.py --email dev@example.com --revoke

Only an active admin (SUPER_ADMIN or ADMIN) is made one: the flag means
nothing on an account that cannot reach the panel. --revoke refuses to take
the last active one away, which would leave the team with nobody to manage
it. Running it again changes nothing. The account is found by its username or
e-mail, case-insensitively.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ADMIN)


async def run(
    username: str | None,
    email: str | None,
    revoke: bool,
    dry_run: bool,
    sessions: async_sessionmaker = async_session,
) -> None:
    async with sessions() as db:
        wanted = []
        if username:
            wanted.append(func.lower(User.username) == username.strip().lower())
        if email:
            wanted.append(func.lower(User.email) == email.strip().lower())
        users = (await db.scalars(select(User).where(or_(*wanted), User.is_guest.is_(False)))).all()
        if len(users) != 1:
            raise SystemExit(f"Refusing: {len(users)} accounts match — nothing written")
        user = users[0]
        name = f"{user.username or '—'} <{user.email}> ({UserRole(user.role).value}, {'aktif' if user.is_active else 'pasif'})"

        if not revoke:
            if user.role not in ADMIN_ROLES or not user.is_active:
                raise SystemExit(f"Refusing: {name} is not an active admin — nothing written")
            if user.is_superadmin:
                print(f"{name}: zaten sistem yöneticisi")
                return
            user.is_superadmin = True
        else:
            if not user.is_superadmin:
                print(f"{name}: zaten sistem yöneticisi değil")
                return
            others = await db.scalar(
                select(func.count()).where(
                    User.is_superadmin.is_(True), User.is_active.is_(True), User.role.in_(ADMIN_ROLES), User.id != user.id
                )
            )
            if not others:
                raise SystemExit(f"Refusing: {name} is the last active Sistem Yöneticisi — nothing written")
            user.is_superadmin = False

        print(f"{name}: sistem yöneticisi {'olmaktan çıkarılıyor' if revoke else 'yapılıyor'}")
        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
        print("written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--username", help="the account's username")
    who.add_argument("--email", help="the account's e-mail address")
    parser.add_argument("--revoke", action="store_true", help="take the flag away instead")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()

    async def go():
        try:
            await run(args.username, args.email, args.revoke, args.dry_run)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
