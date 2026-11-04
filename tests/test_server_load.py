"""What keeps the 1 GB server answering: a small connection pool, and bcrypt
worked off the event loop so a login never stalls everyone else's request."""

import asyncio
import time
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.database import engine
from app.models.user import CustomerSegment, SegmentType, User, UserRole

LOGIN = "/api/v1/auth/login"
PASSWORD = "dogru-sifre-1"


def test_the_pool_is_sized_for_a_small_server():
    pool = engine.sync_engine.pool
    assert pool.size() == 5
    assert pool._max_overflow == 5
    assert pool._timeout == 10
    assert pool._recycle == 300
    assert pool._pre_ping is True


async def _member(db: AsyncSession) -> User:
    seg_id = await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))
    user = User(
        email=f"load-{uuid.uuid4().hex[:8]}@test.com", hashed_password=security.hash_password(PASSWORD),
        role=UserRole.STAFF, segment_id=seg_id, is_active=True,
    )
    db.add(user)
    await db.commit()
    return user


async def _longest_stall(request) -> tuple[float, object]:
    """Run `request` while a ticker wakes every 10 ms; the longest the loop
    kept the ticker waiting, and the request's answer."""
    longest = 0.0
    done = asyncio.Event()

    async def ticker():
        nonlocal longest
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            longest = max(longest, now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        answer = await request()
    finally:
        done.set()
        await task
    return longest, answer


async def test_a_login_does_not_stall_other_requests(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    user = await _member(db_session)
    real_verify = security.verify_password

    def slow_verify(plain: str, hashed: str) -> bool:
        time.sleep(0.5)  # a slow bcrypt round, exaggerated
        return real_verify(plain, hashed)

    monkeypatch.setattr(security, "verify_password", slow_verify)
    stall, answer = await _longest_stall(lambda: client.post(LOGIN, json={"email": user.email, "password": PASSWORD}))
    assert answer.status_code == 200
    # On the loop the ticker would wait the whole half second.
    assert stall < 0.25


async def test_password_helpers_answer_as_the_plain_ones():
    hashed = await security.hash_password_async(PASSWORD)
    assert security.verify_password(PASSWORD, hashed)
    assert await security.verify_password_async(PASSWORD, hashed)
    assert not await security.verify_password_async("yanlis", hashed)
