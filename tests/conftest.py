"""Shared fixtures for integration tests.

Each test gets a fresh engine + session to avoid asyncio event loop conflicts.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.catalog import (
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.user import CustomerSegment, SegmentType, User, UserRole

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

    heavy_prod = Product(base_code=f"HVY-{suffix}", name="Odun Komuru", tags=["HEAVY_CARGO"])
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
