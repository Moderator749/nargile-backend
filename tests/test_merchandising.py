"""A category page's own product order ("Önerilen"), and brands' description and order."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brand import Brand
from app.models.catalog import Category, CategoryProductOrder, Product, ProductCategory
from app.models.user import User
from tests.conftest import auth_header


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
async def cleanup(db_session: AsyncSession, tag: str):
    yield
    await db_session.rollback()
    tagged = select(Category.id).where(Category.slug.like(f"%{tag}%"))
    await db_session.execute(delete(CategoryProductOrder).where(CategoryProductOrder.category_id.in_(tagged)))
    await db_session.execute(Product.__table__.update().where(Product.category_id.in_(tagged)).values(category_id=None))
    await db_session.execute(delete(ProductCategory).where(ProductCategory.category_id.in_(tagged)))
    await db_session.execute(Category.__table__.update().where(Category.slug.like(f"%{tag}%")).values(parent_id=None))
    await db_session.execute(delete(Category).where(Category.slug.like(f"%{tag}%")))
    await db_session.execute(delete(Brand).where(Brand.slug.like(f"%{tag}%")))
    await db_session.commit()


async def _ids(client: AsyncClient, slug: str, **params) -> list[str]:
    return [p["id"] for p in (await client.get("/api/v1/products/", params={"category": slug, **params})).json()]


async def test_a_category_page_follows_its_own_order(
    client: AsyncClient, admin_user: User, catalog, tag: str, cleanup
):
    h = auth_header(admin_user)
    normal_v, heavy_v = catalog
    parent = (await client.post("/api/v1/admin/categories", headers=h, json={"name": f"Grup {tag}"})).json()
    child = (await client.post("/api/v1/admin/categories", headers=h, json={"name": f"Alt {tag}", "parent_id": parent["id"]})).json()
    a, b = str(normal_v.product_id), str(heavy_v.product_id)
    await client.post("/api/v1/admin/products/category", headers=h, json={"product_ids": [a, b], "category_id": child["id"]})

    by_name = await _ids(client, child["slug"])
    assert set(by_name) == {a, b}
    first, second = by_name

    # The subcategory ranks the second one first; the group keeps its own (by name).
    saved = await client.put(f"/api/v1/admin/categories/{child['id']}/product-order", headers=h, json={"product_ids": [second, first]})
    assert saved.status_code == 200, saved.text
    assert [(r["product_id"], r["position"]) for r in saved.json()] == [(second, 0), (first, 1)]
    assert await _ids(client, child["slug"]) == [second, first]
    assert await _ids(client, parent["slug"]) == by_name

    # A ranked product goes before the unranked; "newest" ignores the order.
    await client.put(f"/api/v1/admin/categories/{parent['id']}/product-order", headers=h, json={"product_ids": [second]})
    assert await _ids(client, parent["slug"]) == [second, first]
    rows = (await client.get(f"/api/v1/admin/categories/{parent['id']}/product-order", headers=h)).json()
    assert [r["position"] for r in rows] == [0, None]
    assert set(await _ids(client, parent["slug"], sort="newest")) == {a, b}

    # Clearing it brings back the order by name.
    await client.put(f"/api/v1/admin/categories/{child['id']}/product-order", headers=h, json={"product_ids": []})
    assert await _ids(client, child["slug"]) == by_name

    for bad in ([first, first], [str(uuid.uuid4())]):
        resp = await client.put(f"/api/v1/admin/categories/{child['id']}/product-order", headers=h, json={"product_ids": bad})
        assert resp.status_code == 422, resp.text
    assert (await client.get("/api/v1/admin/categories/999999/product-order", headers=h)).status_code == 404


async def test_the_order_editor_is_for_admins(client: AsyncClient, customer_user: User):
    for method in ("get", "put"):
        resp = await client.request(
            method.upper(), "/api/v1/admin/categories/1/product-order", headers=auth_header(customer_user), json={"product_ids": []}
        )
        assert resp.status_code == 403


async def test_brands_have_a_description_and_an_order(client: AsyncClient, admin_user: User, tag: str, cleanup):
    h = auth_header(admin_user)
    one = (await client.post("/api/v1/admin/brands", headers=h, json={"name": f"Bir {tag}", "description": "  Türk zanaatı.  "})).json()
    two = (await client.post("/api/v1/admin/brands", headers=h, json={"name": f"İki {tag}", "description": "   "})).json()
    assert one["description"] == "Türk zanaatı." and two["description"] is None
    public = {b["id"]: b for b in (await client.get("/api/v1/brands/")).json()}
    assert public[one["id"]]["description"] == "Türk zanaatı."

    everyone = [b["id"] for b in (await client.get("/api/v1/admin/brands", headers=h)).json()]
    wanted = [two["id"], one["id"]] + [i for i in everyone if i not in (one["id"], two["id"])]
    ordered = await client.put("/api/v1/admin/brands/order", headers=h, json={"ids": wanted})
    assert ordered.status_code == 200, ordered.text
    assert [b["id"] for b in ordered.json()][:2] == [two["id"], one["id"]]
    assert (await client.put("/api/v1/admin/brands/order", headers=h, json={"ids": [one["id"], one["id"]]})).status_code == 422
    assert (await client.put("/api/v1/admin/brands/order", headers=h, json={"ids": [999999]})).status_code == 404
