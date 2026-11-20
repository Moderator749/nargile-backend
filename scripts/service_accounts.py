"""Service accounts (app.models.service_account): create, list, rotate, revoke.

    venv/bin/python scripts/service_accounts.py create --name radar --scope price:read
    venv/bin/python scripts/service_accounts.py list
    venv/bin/python scripts/service_accounts.py rotate --name radar
    venv/bin/python scripts/service_accounts.py revoke --name radar

`create` and `rotate` print the token once: it is kept only as a hash, so it
cannot be shown again. Rotating ends the old token at once. A revoked account
stays listed (its name is taken) and is never opened again; create another.
Run on the server, as the service user, from the backend's directory.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.api.service_auth import new_token, token_hash  # noqa: E402
from app.core.database import async_session, engine  # noqa: E402
from app.models.service_account import SCOPES, ServiceAccount  # noqa: E402


def _show(token: str) -> None:
    print("\nToken (shown once — store it now, e.g. in the service's credentials file):\n")
    print(f"  {token}\n")


async def _named(db, name: str) -> ServiceAccount:
    account = await db.scalar(select(ServiceAccount).where(ServiceAccount.name == name))
    if account is None:
        raise SystemExit(f"No service account named {name!r}")
    return account


async def run(command: str, name: str | None, scopes: list[str], sessions: async_sessionmaker = async_session) -> str | None:
    """Do it; returns the new token for create/rotate (the tests read it)."""
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        if command == "list":
            accounts = (await db.scalars(select(ServiceAccount).order_by(ServiceAccount.id))).all()
            if not accounts:
                print("No service accounts.")
            for a in accounts:
                state = f"REVOKED {a.revoked_at:%Y-%m-%d %H:%M}" if a.revoked_at else "active"
                used = f"{a.last_used_at:%Y-%m-%d %H:%M}" if a.last_used_at else "never"
                print(f"{a.name:<20} {a.token_prefix}…  {','.join(a.scopes):<20} {state:<24} last used {used}")
            return None

        if command == "create":
            unknown = [s for s in scopes if s not in SCOPES]
            if not scopes or unknown:
                raise SystemExit(f"Scopes must be some of {', '.join(SCOPES)} (got {', '.join(scopes) or 'none'})")
            if await db.scalar(select(ServiceAccount.id).where(ServiceAccount.name == name)):
                raise SystemExit(f"A service account named {name!r} exists already")
            token = new_token()
            db.add(ServiceAccount(
                name=name, token_hash=token_hash(token), token_prefix=token[:8], scopes=sorted(set(scopes)), created_at=now,
            ))
            await db.commit()
            print(f"Created {name!r} with {', '.join(sorted(set(scopes)))}")
            _show(token)
            return token

        account = await _named(db, name)
        if account.revoked_at is not None:
            raise SystemExit(f"{name!r} is revoked; create a new account instead")
        if command == "rotate":
            token = new_token()
            account.token_hash, account.token_prefix, account.rotated_at = token_hash(token), token[:8], now
            await db.commit()
            print(f"Rotated {name!r}: the old token no longer works")
            _show(token)
            return token
        if command == "revoke":
            account.revoked_at = now
            await db.commit()
            print(f"Revoked {name!r}")
            return None
    raise SystemExit(f"Unknown command {command!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("create", "list", "rotate", "revoke"))
    parser.add_argument("--name", help="the account's name, e.g. radar")
    parser.add_argument("--scope", action="append", default=[], help=f"with create; one of {', '.join(SCOPES)}; repeatable")
    args = parser.parse_args()
    if args.command != "list" and not args.name:
        parser.error("--name is required")

    async def go():
        try:
            await run(args.command, args.name, args.scope)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
