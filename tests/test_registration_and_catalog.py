"""Tests for self-service registration hardening and segment-aware catalog pricing."""

import json
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from tests.conftest import auth_header


async def _register(client: AsyncClient, **extra) -> dict:
    payload = {"email": f"reg-{uuid.uuid4().hex[:8]}@test.com", "password": "Secret123!", **extra}
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    return payload


async def _me(client: AsyncClient, email: str, password: str) -> dict:
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    token = login.json()["access_token"]
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    return resp.json()


# ── Registration privilege escalation ────────────────────────────────────────


@pytest.mark.parametrize("requested", ["DEALER", "WHOLESALE", "CAFE"])
async def test_register_ignores_client_segment(client: AsyncClient, requested: str):
    creds = await _register(client, segment_code=requested)
    me = await _me(client, creds["email"], creds["password"])
    assert me["segment_code"] == "RETAIL"
    assert me["role"] == "CUSTOMER"


async def test_register_ignores_client_role(client: AsyncClient):
    creds = await _register(client, role="SUPER_ADMIN")
    me = await _me(client, creds["email"], creds["password"])
    assert me["role"] == "CUSTOMER"


async def test_register_stores_full_name(client: AsyncClient):
    creds = await _register(client, full_name="  Ayşe Yılmaz  ")
    me = await _me(client, creds["email"], creds["password"])
    assert me["full_name"] == "Ayşe Yılmaz"


# ── Catalog pricing per segment ──────────────────────────────────────────────


@pytest.fixture
async def priced_variant(
    db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]
) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"CAT-{suffix}", name=f"Catalog Test {suffix}", tags=[])
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"CAT-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    for code, incl in ((SegmentType.RETAIL, "1200.00"), (SegmentType.DEALER, "800.00")):
        db_session.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(Decimal(incl) / Decimal("1.2")).quantize(Decimal("0.01")),
            price_incl_tax=Decimal(incl), vat_rate=Decimal("20.00"),
        ))
    await db_session.commit()
    return variant


@pytest.fixture
async def dealer_user(
    db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]
) -> User:
    user = User(
        email=f"dealer-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="not-a-real-hash",
        role=UserRole.CUSTOMER,
        segment_id=segments[SegmentType.DEALER].id,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _find_variant(client: AsyncClient, variant_id: uuid.UUID, headers=None) -> dict:
    skip = 0
    while True:
        resp = await client.get(
            "/api/v1/products/", params={"skip": skip, "limit": 100}, headers=headers
        )
        assert resp.status_code == 200
        page = resp.json()
        for product in page:
            for v in product["variants"]:
                if v["id"] == str(variant_id):
                    return v
        if len(page) < 100:
            raise AssertionError("variant not found in catalog")
        skip += 100


async def test_catalog_anonymous_gets_retail_price(client: AsyncClient, priced_variant):
    v = await _find_variant(client, priced_variant.id)
    assert v["price"]["price_incl_tax"] == "1200.00"
    assert v["list_price"]["price_incl_tax"] == "1200.00"


async def test_catalog_dealer_gets_dealer_price(
    client: AsyncClient, priced_variant, dealer_user: User
):
    v = await _find_variant(client, priced_variant.id, headers=auth_header(dealer_user))
    assert v["price"]["price_incl_tax"] == "800.00"
    assert v["list_price"]["price_incl_tax"] == "1200.00"


async def test_catalog_never_exposes_other_segment_prices(client: AsyncClient, priced_variant):
    v = await _find_variant(client, priced_variant.id)
    assert set(v) == {"id", "sku", "attributes", "price", "list_price"}
    assert "800.00" not in json.dumps(v)


async def test_catalog_rejects_invalid_token(client: AsyncClient):
    resp = await client.get(
        "/api/v1/products/", headers={"Authorization": "Bearer this.is.garbage"}
    )
    assert resp.status_code == 401


async def test_catalog_limit_is_capped(client: AsyncClient):
    resp = await client.get("/api/v1/products/", params={"limit": 1000})
    assert resp.status_code == 422
