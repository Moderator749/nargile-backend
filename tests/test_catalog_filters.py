"""Tests for the public categories endpoint and catalog category/search filters."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product


@pytest.fixture
async def filter_fixture(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Filtre {suffix}", slug=f"filtre-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    inside = Product(
        base_code=f"FLT-{suffix}", name=f"ÖZEL NARGİLE {suffix}", category_id=cat.id, tags=[],
        image_url=f"https://www.nargilemalzemesi.com/image/cache/catalog/{suffix}-800x800.jpg",
    )
    outside = Product(base_code=f"OUT-{suffix}", name=f"Diğer Ürün {suffix}", tags=[])
    literal = Product(base_code=f"PCT-{suffix}", name=f"Yüzde 100% {suffix}", tags=[])
    db_session.add_all([inside, outside, literal])
    await db_session.commit()
    yield cat, inside, outside, literal, suffix

    # Clean up: these rows would otherwise appear as real catalog tabs/products.
    await db_session.execute(delete(Product).where(Product.id.in_([inside.id, outside.id, literal.id])))
    await db_session.execute(delete(Category).where(Category.id == cat.id))
    await db_session.commit()


async def _ids(client: AsyncClient, **params) -> set[str]:
    resp = await client.get("/api/v1/products/", params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return {p["id"] for p in resp.json()}


async def test_list_categories_is_public(client: AsyncClient, filter_fixture):
    cat, *_ = filter_fixture
    resp = await client.get("/api/v1/categories/")
    assert resp.status_code == 200
    assert {"id": cat.id, "name": cat.name, "slug": cat.slug, "parent_id": None, "sort_order": 0} in resp.json()


async def test_filter_by_category_slug(client: AsyncClient, filter_fixture):
    cat, inside, outside, *_ = filter_fixture
    ids = await _ids(client, category=cat.slug)
    assert ids == {str(inside.id)}


async def test_unknown_category_returns_empty(client: AsyncClient):
    assert await _ids(client, category=f"yok-{uuid.uuid4().hex}") == set()


@pytest.mark.parametrize("term", ["özel nargile", "ÖZEL NARGİLE", "Özel Nargıle"])
async def test_search_folds_turkish_case(client: AsyncClient, filter_fixture, term: str):
    _, inside, _, _, suffix = filter_fixture
    assert str(inside.id) in await _ids(client, q=f"{term} {suffix}")


async def test_search_treats_wildcards_literally(client: AsyncClient, filter_fixture):
    _, inside, outside, literal, suffix = filter_fixture
    ids = await _ids(client, q=f"100% {suffix}")
    assert ids == {str(literal.id)}
    # A bare "%" must not match everything.
    assert str(outside.id) not in await _ids(client, q=f"%{suffix[:0]}_")


async def test_search_and_category_combine(client: AsyncClient, filter_fixture):
    cat, inside, *_ , suffix = filter_fixture
    assert await _ids(client, category=cat.slug, q=suffix) == {str(inside.id)}
    assert await _ids(client, category=cat.slug, q="bulunmayan-terim") == set()


async def test_catalog_exposes_product_image_url(client: AsyncClient, filter_fixture):
    _cat, inside, outside, _literal, suffix = filter_fixture
    resp = await client.get("/api/v1/products/", params={"q": suffix, "limit": 100})
    images = {p["id"]: p["image_url"] for p in resp.json()}
    assert images[str(inside.id)] == inside.image_url
    assert images[str(outside.id)] is None
