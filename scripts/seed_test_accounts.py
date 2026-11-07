"""The shop's test customer accounts, one per price list and account kind,
created or brought back to what they should be.

    SEED_TEST_PASSWORD='…' .venv/bin/python scripts/seed_test_accounts.py --dry-run
    SEED_TEST_PASSWORD='…' .venv/bin/python scripts/seed_test_accounts.py

The password comes from --password or SEED_TEST_PASSWORD, never from this
file: the repository is not where a live login belongs.

Each account is a verified, active customer on its price list, with the
account type that list is reached by (kafe, bayi and toptan as businesses,
yurt dışı as an export account, the two branches as bireysel/perakende).
Running it again changes only what has drifted: a password that already
matches is left alone, so their sessions survive; a changed one ends them.

It never takes over an account that is not a test account: an address that
belongs to a member of the panel team, or to a customer with orders, is
refused and nothing is written.
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.core.security import CUSTOMER_PASSWORD_MIN, hash_password_async, verify_password_async  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.user import AccountType, CustomerSegment, SegmentType, User, UserRole  # noqa: E402


@dataclass(frozen=True)
class TestAccount:
    email: str
    name: str
    account_type: AccountType
    segment: SegmentType


ACCOUNTS: tuple[TestAccount, ...] = (
    TestAccount("cafe@sultannargile.com", "Test Cafe", AccountType.CAFE, SegmentType.CAFE),
    TestAccount("bayi@sultannargile.com", "Test Bayi", AccountType.TOBACCO_SHOP, SegmentType.DEALER),
    TestAccount("toptan@sultannargile.com", "Test Toptan", AccountType.TOBACCO_SHOP, SegmentType.WHOLESALE),
    # Export: the account type is what makes it one (no payment screen, a quote);
    # wholesale prices are what an approved export customer buys at.
    TestAccount("yurtdisi@sultannargile.com", "Test Yurtdışı", AccountType.INTERNATIONAL, SegmentType.WHOLESALE),
    TestAccount("esenyurt@sultannargile.com", "Test Esenyurt Şube", AccountType.INDIVIDUAL, SegmentType.RETAIL),
    TestAccount("bahcelievler@sultannargile.com", "Test Bahçelievler Şube", AccountType.INDIVIDUAL, SegmentType.RETAIL),
)

SEGMENT_LABELS = {
    SegmentType.RETAIL: "Perakende",
    SegmentType.CAFE: "Kafe",
    SegmentType.WHOLESALE: "Toptan",
    SegmentType.DEALER: "Bayi",
}


@dataclass
class Row:
    account: TestAccount
    action: str  # "created" | "updated" | "unchanged"
    changed: list[str]
    active: bool
    verified: bool


async def run(password: str, dry_run: bool, sessions: async_sessionmaker = async_session) -> list[Row]:
    if len(password) < CUSTOMER_PASSWORD_MIN:
        raise SystemExit(f"Refusing: the password is shorter than {CUSTOMER_PASSWORD_MIN} characters")
    async with sessions() as db:
        segments = {s.code: s.id for s in (await db.scalars(select(CustomerSegment))).all()}
        missing = [s.value for s in SEGMENT_LABELS if s not in segments]
        if missing:
            raise SystemExit(f"Refusing: no price list {', '.join(missing)} in this database — nothing written")

        # Every refusal is found before anything is written.
        found: dict[str, User] = {}
        for account in ACCOUNTS:
            user = await db.scalar(
                select(User).where(func.lower(User.email) == account.email, User.is_guest.is_(False))
            )
            if user is None:
                continue
            if user.role != UserRole.CUSTOMER:
                raise SystemExit(f"Refusing: {account.email} is a panel account ({UserRole(user.role).value}) — nothing written")
            orders = await db.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id))
            if orders:
                raise SystemExit(f"Refusing: {account.email} has {orders} orders, so it is not a test account — nothing written")
            found[account.email] = user

        rows: list[Row] = []
        hashed: str | None = None
        for account in ACCOUNTS:
            user = found.get(account.email)
            if user is None:
                hashed = hashed or await hash_password_async(password)
                user = User(
                    email=account.email,
                    full_name=account.name,
                    hashed_password=hashed,
                    role=UserRole.CUSTOMER,
                    account_type=account.account_type,
                    segment_id=segments[account.segment],
                    is_active=True,
                    is_verified=True,
                )
                db.add(user)
                rows.append(Row(account, "created", [], True, True))
                continue

            wanted = {
                "full_name": account.name,
                "account_type": account.account_type,
                "segment_id": segments[account.segment],
                "is_active": True,
                "is_verified": True,
            }
            changed = [field for field, value in wanted.items() if getattr(user, field) != value]
            for field in changed:
                setattr(user, field, wanted[field])
            if not await verify_password_async(password, user.hashed_password):
                hashed = hashed or await hash_password_async(password)
                user.hashed_password = hashed
                # As a password reset does: sessions on the old password end.
                user.token_version = (user.token_version or 0) + 1
                changed.append("password")
            rows.append(Row(account, "updated" if changed else "unchanged", changed, True, True))

        await db.flush()
        if dry_run:
            await db.rollback()
        else:
            await db.commit()
        return rows


def table(rows: list[Row]) -> str:
    head = ("E-posta", "Ad", "Hesap türü", "Fiyat grubu", "Aktif", "Doğrulanmış", "İşlem")
    body = [
        (
            r.account.email,
            r.account.name,
            r.account.account_type.value,
            SEGMENT_LABELS[r.account.segment],
            "evet" if r.active else "hayır",
            "evet" if r.verified else "hayır",
            r.action + (f" ({', '.join(r.changed)})" if r.changed else ""),
        )
        for r in rows
    ]
    widths = [max(len(str(line[i])) for line in (head, *body)) for i in range(len(head))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*head), fmt.format(*("─" * w for w in widths)), *(fmt.format(*line) for line in body)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--password", default=os.environ.get("SEED_TEST_PASSWORD"), help="default: $SEED_TEST_PASSWORD")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()
    if not args.password:
        parser.error("give the password with --password or SEED_TEST_PASSWORD")

    async def go():
        try:
            rows = await run(args.password, args.dry_run)
        finally:
            await engine.dispose()
        print(table(rows))
        print("dry run: nothing written" if args.dry_run else "written")

    asyncio.run(go())


if __name__ == "__main__":
    main()
