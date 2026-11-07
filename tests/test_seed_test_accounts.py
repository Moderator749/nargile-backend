"""scripts/seed_test_accounts.py: the test customers, created once and kept as
they should be, and never an account that is not a test account."""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scripts.seed_test_accounts as script
from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import AccountType, CustomerSegment, SegmentType, User, UserRole

PASSWORD = "test-sifre-1."


@pytest.fixture
def accounts(monkeypatch, segments):
    """This run's own addresses: the test database outlives the run."""
    suffix = uuid.uuid4().hex[:8]
    mine = tuple(
        script.TestAccount(a.email.replace("@", f"+{suffix}@"), a.name, a.account_type, a.segment) for a in script.ACCOUNTS
    )
    monkeypatch.setattr(script, "ACCOUNTS", mine)
    return mine


@pytest.fixture
def sessions(db_session: AsyncSession) -> async_sessionmaker:
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


async def _user(db: AsyncSession, email: str) -> User:
    return await db.scalar(select(User).where(User.email == email).execution_options(populate_existing=True))


async def test_the_accounts_are_made_and_can_sign_in(
    client: AsyncClient, db_session: AsyncSession, sessions, accounts, segments: dict[SegmentType, CustomerSegment]
):
    rows = await script.run(PASSWORD, dry_run=False, sessions=sessions)
    assert [r.action for r in rows] == ["created"] * 6

    for account in accounts:
        user = await _user(db_session, account.email)
        assert (user.full_name, user.role, user.account_type, user.segment_id, user.is_active, user.is_verified) == (
            account.name, UserRole.CUSTOMER, account.account_type, segments[account.segment].id, True, True,
        )
        # Signed in with the password as given, and on the price list it was given.
        login = await client.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
        assert me.json()["segment_code"] == account.segment.value

    # The export account is one by its type.
    assert (await _user(db_session, accounts[3].email)).account_type == AccountType.INTERNATIONAL


async def test_a_dry_run_writes_nothing(db_session: AsyncSession, sessions, accounts):
    rows = await script.run(PASSWORD, dry_run=True, sessions=sessions)
    assert [r.action for r in rows] == ["created"] * 6
    assert await _user(db_session, accounts[0].email) is None


async def test_running_it_again_changes_only_what_drifted(db_session: AsyncSession, sessions, accounts, segments):
    await script.run(PASSWORD, dry_run=False, sessions=sessions)
    again = await script.run(PASSWORD, dry_run=False, sessions=sessions)
    assert [r.action for r in again] == ["unchanged"] * 6

    # Someone moved one, switched one off, and changed a password.
    bayi = await _user(db_session, accounts[1].email)
    bayi.segment_id = segments[SegmentType.RETAIL].id
    sube = await _user(db_session, accounts[4].email)
    sube.is_active = False
    versions = sube.token_version
    await db_session.commit()

    fixed = await script.run(PASSWORD, dry_run=False, sessions=sessions)
    by_email = {r.account.email: r for r in fixed}
    assert (by_email[accounts[1].email].action, by_email[accounts[1].email].changed) == ("updated", ["segment_id"])
    assert by_email[accounts[4].email].changed == ["is_active"]
    assert (await _user(db_session, accounts[1].email)).segment_id == segments[SegmentType.DEALER].id
    assert (await _user(db_session, accounts[4].email)).token_version == versions  # same password: sessions kept

    reset = await script.run(PASSWORD + "x", dry_run=False, sessions=sessions)
    assert all(r.changed == ["password"] for r in reset)
    assert (await _user(db_session, accounts[4].email)).token_version == versions + 1


async def test_a_panel_account_is_never_taken_over(db_session: AsyncSession, sessions, accounts, segments):
    db_session.add(User(email=accounts[2].email, hashed_password="x", role=UserRole.ADMIN, is_active=True))
    await db_session.commit()
    with pytest.raises(SystemExit, match="panel account"):
        await script.run(PASSWORD, dry_run=False, sessions=sessions)
    # Not even the others were written.
    assert await _user(db_session, accounts[0].email) is None


async def test_a_customer_with_orders_is_never_taken_over(db_session: AsyncSession, sessions, accounts, segments):
    real = User(email=accounts[5].email, hashed_password="x", role=UserRole.CUSTOMER, is_active=True)
    db_session.add(real)
    await db_session.flush()
    db_session.add(Order(
        order_number=f"SEED-{uuid.uuid4().hex[:8].upper()}",
        user_id=real.id,
        total_amount=Decimal("100.00"),
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=OrderStatus.PENDING_APPROVAL,
    ))
    await db_session.commit()
    with pytest.raises(SystemExit, match="has 1 orders"):
        await script.run(PASSWORD, dry_run=False, sessions=sessions)
    assert (await _user(db_session, accounts[5].email)).hashed_password == "x"


async def test_a_short_password_is_refused(sessions, accounts):
    with pytest.raises(SystemExit, match="shorter"):
        await script.run("12345", dry_run=False, sessions=sessions)
