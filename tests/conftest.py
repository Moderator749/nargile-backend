"""Shared fixtures for integration tests.

Tests run against their own PostgreSQL database (``<dev db>_test``, or
TEST_DATABASE_URL), rebuilt from the Alembic migrations at the start of every
session. They never touch the dev/prod database.

Each test gets a fresh engine + session to avoid asyncio event loop conflicts.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from app.core.config import settings

# ── Point everything at the test database BEFORE anything creates an engine ──
_DEV_URL = make_url(settings.DATABASE_URL)
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _DEV_URL.set(
    database=f"{_DEV_URL.database}_test"
).render_as_string(hide_password=False)
if make_url(TEST_DATABASE_URL).database == _DEV_URL.database:
    raise RuntimeError("TEST_DATABASE_URL must not point at the dev/prod database")
settings.DATABASE_URL = TEST_DATABASE_URL
# Card checkout needs the gateway configured; these are dummies, and nothing in
# the suite reaches PayTR (its HTTP call is replaced where it is exercised).
settings.PAYTR_MERCHANT_ID = "100000"
settings.PAYTR_MERCHANT_KEY = "test-merchant-key"
settings.PAYTR_MERCHANT_SALT = "test-merchant-salt"
settings.PAYTR_TEST_MODE = True
# Never Yurtiçi's real accounts, whatever .env holds: the suite runs as an
# unconfigured shop, and the tests that ship through the web service use a
# stand-in provider (tests/test_shipping.py).
for _name in (
    "YURTICI_MUSTERI_KODU", "YURTICI_API_USER_GO", "YURTICI_API_PASS_GO", "YURTICI_API_USER_AO",
    "YURTICI_API_PASS_AO", "YURTICI_API_USER", "YURTICI_API_PASSWORD", "YURTICI_CUSTOMER_CODE",
):
    setattr(settings, _name, "")
settings.YURTICI_TEST_MODE = True

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.database import get_db  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.main import app  # noqa: E402
from app.models.catalog import (  # noqa: E402
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.user import CustomerSegment, SegmentType, User, UserRole  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]

# A valid delivery address for checkout calls (required to place an order).
# The two agreements every order has to carry: an order is a contract, and the
# engine refuses one that was not accepted. Price previews need neither.
CONSENTS = {"distance_sales_accepted": True, "terms_accepted": True}

ADDRESS = {
    "full_name": "Test Müşteri",
    "phone": "0532 000 00 00",
    "city": "Ankara",
    "district": "Çankaya",
    "line": "Atatürk Bulvarı No: 1",
}
SEGMENTS = (
    (SegmentType.RETAIL, "Perakende"),
    (SegmentType.CAFE, "Kafe"),
    (SegmentType.WHOLESALE, "Toptan"),
    (SegmentType.DEALER, "Bayi"),
)


async def _recreate_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = await asyncpg.connect(
        user=url.username, password=url.password, host=url.host, port=url.port or 5432, database="postgres"
    )
    try:
        # Identifier comes from our own config, not user input; quote it anyway.
        name = url.database.replace('"', '""')
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


async def _seed_segments() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with async_sessionmaker(engine)() as session:
        for code, label in SEGMENTS:
            session.add(CustomerSegment(code=code, label=label))
        await session.commit()
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Fresh schema every session: drop/create the test DB, migrate, seed segments."""
    asyncio.run(_recreate_test_database())
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        check=True,
        capture_output=True,
    )
    asyncio.run(_seed_segments())
    yield

# ---------------------------------------------------------------------------
# Fresh engine + session per test (avoids event-loop contamination)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def segments(db_session: AsyncSession) -> dict[SegmentType, CustomerSegment]:
    result = await db_session.execute(select(CustomerSegment))
    return {seg.code: seg for seg in result.scalars().all()}


@pytest.fixture
async def admin_user(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
) -> User:
    user = User(
        email=f"admin-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="not-a-real-hash",
        role=UserRole.SUPER_ADMIN,
        segment_id=segments[SegmentType.RETAIL].id,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def customer_user(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
) -> User:
    user = User(
        email=f"customer-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="not-a-real-hash",
        role=UserRole.CUSTOMER,
        segment_id=segments[SegmentType.RETAIL].id,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def make_token(user: User) -> str:
    return create_access_token({"sub": str(user.id), "role": user.role})


def auth_header(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user)}"}


# ---------------------------------------------------------------------------
# Catalog seed
# ---------------------------------------------------------------------------


@pytest.fixture
async def catalog(
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    retail_id = segments[SegmentType.RETAIL].id
    suffix = uuid.uuid4().hex[:6]

    prod = Product(base_code=f"NRM-{suffix}", name="Normal Nargile", tags=[])
    db_session.add(prod)
    await db_session.flush()

    normal_v = ProductVariant(product_id=prod.id, sku=f"NRM-{suffix}")
    db_session.add(normal_v)
    await db_session.flush()

    db_session.add(VariantPrice(
        variant_id=normal_v.id, segment_id=retail_id, currency="TRY",
        price_excl_tax=Decimal("4237.29"), price_incl_tax=Decimal("5000.00"),
        vat_rate=Decimal("18.00"),
    ))
    db_session.add(Inventory(
        variant_id=normal_v.id,
        warehouse_type=WarehouseType.RETAIL_STOCK, quantity=500,
    ))

    # Coal: its "Sepet Kuralı" switch is on, so the coal cargo rules apply.
    heavy_prod = Product(
        base_code=f"HVY-{suffix}", name="Odun Komuru", tags=["HEAVY_CARGO"], is_cart_rule_applied=True
    )
    db_session.add(heavy_prod)
    await db_session.flush()

    heavy_v = ProductVariant(product_id=heavy_prod.id, sku=f"HVY-{suffix}")
    db_session.add(heavy_v)
    await db_session.flush()

    db_session.add(VariantPrice(
        variant_id=heavy_v.id, segment_id=retail_id, currency="TRY",
        price_excl_tax=Decimal("847.46"), price_incl_tax=Decimal("1000.00"),
        vat_rate=Decimal("18.00"),
    ))
    db_session.add(Inventory(
        variant_id=heavy_v.id,
        warehouse_type=WarehouseType.RETAIL_STOCK, quantity=500,
    ))

    await db_session.commit()
    return normal_v, heavy_v
