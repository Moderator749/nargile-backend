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
    entry = next(c for c in resp.json() if c["id"] == cat.id)
    assert {k: entry[k] for k in ("name", "slug", "parent_id", "sort_order", "image_url")} == {
        "name": cat.name, "slug": cat.slug, "parent_id": None, "sort_order": 0, "image_url": None,
    }
    # The one product filed under it, dated by its own last edit: the sitemap's
    # <lastmod> for the category page.
    assert entry["product_count"] == 1
    assert entry["updated_at"] is not None


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


async def test_catalog_exposes_when_a_product_last_changed(
    client: AsyncClient, db_session: AsyncSession, filter_fixture
):
    """`updated_at` is the sitemap's <lastmod>: present on every product, and
    moved forward by an edit to the product row."""
    from datetime import datetime

    _cat, inside, _outside, _literal, suffix = filter_fixture

    async def stamp() -> datetime:
        resp = await client.get("/api/v1/products/", params={"q": suffix, "limit": 100})
        found = {p["id"]: p["updated_at"] for p in resp.json()}
        assert found[str(inside.id)], "every product carries updated_at"
        return datetime.fromisoformat(found[str(inside.id)])

    before = await stamp()
    inside.description = "Yeni açıklama"
    await db_session.commit()
    assert await stamp() > before


@pytest.mark.parametrize("term", ["ozel nargile", "OZEL NARGILE", "özel nargıle"])
async def test_search_folds_turkish_letters(client: AsyncClient, filter_fixture, term: str):
    """A phone's keyboard types "lule" for "lüle": the letters fold to ASCII on both sides."""
    _, inside, _, _, suffix = filter_fixture
    assert str(inside.id) in await _ids(client, q=f"{term} {suffix}")


async def test_search_finds_a_category_by_its_name(client: AsyncClient, db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Lüleler {suffix}", slug=f"luleler-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    filed = Product(base_code=f"KLR-{suffix}", name=f"Killer {suffix}", category_id=cat.id, tags=[])
    db_session.add(filed)
    await db_session.commit()
    try:
        assert str(filed.id) in await _ids(client, q=f"luleler {suffix}")
    finally:
        await db_session.execute(delete(Product).where(Product.id == filed.id))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()


async def test_sold_out_products_come_last(client: AsyncClient, db_session: AsyncSession):
    """Whatever the order, a product with no colour in the caller's stock sinks to the end."""
    from app.models.catalog import Inventory, ProductVariant, WarehouseType

    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Stok {suffix}", slug=f"stok-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    products = {
        name: Product(base_code=f"{name}-{suffix}", name=f"{name} {suffix}", category_id=cat.id, tags=[])
        for name in ("Aaa", "Bbb", "Ccc")
    }
    db_session.add_all(products.values())
    await db_session.flush()
    for name, quantity in (("Aaa", None), ("Bbb", 5), ("Ccc", 0)):
        variant = ProductVariant(product_id=products[name].id, sku=f"{name}-{suffix}")
        db_session.add(variant)
        await db_session.flush()
        if quantity is not None:
            db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=quantity))
    await db_session.commit()
    try:
        resp = await client.get("/api/v1/products/", params={"category": cat.slug})
        assert [p["name"] for p in resp.json()] == [f"Bbb {suffix}", f"Aaa {suffix}", f"Ccc {suffix}"]
    finally:
        ids = [p.id for p in products.values()]
        await db_session.execute(delete(Inventory).where(Inventory.variant_id.in_(
            ProductVariant.__table__.select().with_only_columns(ProductVariant.id).where(ProductVariant.product_id.in_(ids))
        )))
        await db_session.execute(delete(ProductVariant).where(ProductVariant.product_id.in_(ids)))
        await db_session.execute(delete(Product).where(Product.id.in_(ids)))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()


async def test_cart_lines_link_a_visitors_lines_without_a_count(client: AsyncClient, catalog):
    """A guest's cart learns each line's page, code and whether any is left — never how many."""
    normal_v, heavy_v = catalog
    resp = await client.get("/api/v1/products/cart-lines", params={"ids": [str(normal_v.id), str(uuid.uuid4())]})
    assert resp.status_code == 200, resp.text
    [line] = resp.json()
    assert line == {
        "variant_id": str(normal_v.id),
        "product_id": line["product_id"],
        "product_slug": line["product_slug"],
        "sku": normal_v.sku,
        "in_stock": True,
    }
    assert line["product_slug"]


async def test_stock_check_holds_a_quantity_to_the_stock_without_the_count(client: AsyncClient, catalog):
    normal_v, heavy_v = catalog
    gone = str(uuid.uuid4())
    resp = await client.post("/api/v1/products/stock-check", json={"items": [
        {"variant_id": str(normal_v.id), "quantity": 3},
        {"variant_id": str(heavy_v.id), "quantity": 9999},
        {"variant_id": gone, "quantity": 2},
    ]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"variant_id": str(normal_v.id), "quantity": 3, "capped": False},
        {"variant_id": str(heavy_v.id), "quantity": 500, "capped": True},
        {"variant_id": gone, "quantity": 0, "capped": True},
    ]


async def test_suggestions_fold_turkish_letters(client: AsyncClient, db_session: AsyncSession):
    """From the second letter: products as the search lists them, the matching categories, the total."""
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Lüleler {suffix}", slug=f"luleler-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    killer = Product(base_code=f"KL-{suffix}", name=f"Sırlı Lüle {suffix}", category_id=cat.id, tags=[])
    db_session.add(killer)
    await db_session.commit()
    try:
        resp = await client.get("/api/v1/products/suggest", params={"q": f"lule {suffix}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert [p["id"] for p in body["products"]] == [str(killer.id)]
        resp = await client.get("/api/v1/products/suggest", params={"q": f"luleler {suffix}"})
        assert resp.json()["categories"] == [{"name": cat.name, "slug": cat.slug}]
        assert (await client.get("/api/v1/products/suggest", params={"q": "l"})).status_code == 422
    finally:
        await db_session.execute(delete(Product).where(Product.id == killer.id))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()
