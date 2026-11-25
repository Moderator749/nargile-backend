"""Group access rules ("Grup Erişim Kısıtlamaları"): a tier's hidden
categories (with their subcategories), brands and pages are kept out of every
storefront answer, refused by their own endpoints, and not sold; other tiers
and the admin see everything."""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brand import Brand
from app.models.catalog import Category, Inventory, Product, ProductCategory, ProductVariant, VariantPrice, WarehouseType
from app.models.user import CustomerSegment, SegmentType, User
from app.services import tier_access
from tests.conftest import auth_header
from tests.test_b2b_havale import _firm, _quote

ADMIN = "/api/v1/admin/access-rules"
RESTRICTED = "hesap türünüz için görüntülemeye kapalıdır"


@pytest.fixture
async def world(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]):
    """A parent category with a child, a brand, and three products: one filed
    under the child only (as a second category), one of the brand, one free."""
    s = uuid.uuid4().hex[:6]
    parent = Category(name=f"Gizli {s}", slug=f"gizli-{s}")
    db_session.add(parent)
    await db_session.flush()
    child = Category(name=f"Alt {s}", slug=f"alt-{s}", parent_id=parent.id)
    other = Category(name=f"Açık {s}", slug=f"acik-{s}")
    brand = Brand(name=f"Rakip {s}", slug=f"rakip-{s}", search_term=f"rakip {s}")
    db_session.add_all([child, other, brand])
    await db_session.flush()

    def product(tag: str, **kw) -> Product:
        p = Product(base_code=f"TA-{tag}-{s}", name=f"Ürün {tag} {s}", slug=f"urun-{tag}-{s}", tags=[], is_published=True, **kw)
        db_session.add(p)
        return p

    in_child = product("kat", category_id=other.id)  # primary elsewhere, listed under the hidden child too
    of_brand = product("mar", brand=brand.name)  # the name links brand_id (catalog._link_product_brand)
    free = product("ser", category_id=other.id)
    await db_session.flush()
    db_session.add(ProductCategory(product_id=in_child.id, category_id=child.id))
    variants = {}
    for p in (in_child, of_brand, free):
        v = ProductVariant(product_id=p.id, sku=p.base_code)
        db_session.add(v)
        await db_session.flush()
        variants[p.id] = v.id
        for seg in segments.values():
            db_session.add(VariantPrice(
                variant_id=v.id, segment_id=seg.id, currency="TRY",
                price_excl_tax=Decimal("100.00"), price_incl_tax=Decimal("120.00"), vat_rate=Decimal("20.00"),
            ))
        db_session.add(Inventory(variant_id=v.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=50))
    await db_session.commit()
    return {
        "s": s, "parent": parent.id, "child_slug": child.slug, "other_slug": other.slug, "brand": brand.id,
        "brand_slug": brand.slug, "in_child": in_child, "of_brand": of_brand, "free": free, "variants": variants,
    }


@pytest.fixture
async def rules(client: AsyncClient, admin_user: User):
    """Sets rules through the admin API; every tier's set is emptied afterwards,
    so no other test sees them."""
    h = auth_header(admin_user)

    async def put(tier: str, **body):
        resp = await client.put(f"{ADMIN}/{tier}", headers=h, json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    yield put
    for tier in ("GUEST", "RETAIL", "CAFE", "WHOLESALE", "DEALER"):
        await client.put(f"{ADMIN}/{tier}", headers=h, json={})
    tier_access.invalidate()


def _ids(listing) -> set[str]:
    return {p["id"] for p in listing}


async def test_a_tiers_hidden_category_and_brand_are_gone_for_it_only(
    client: AsyncClient, db_session: AsyncSession, segments, world, rules, customer_user: User, admin_user: User
):
    saved = await rules("WHOLESALE", category_ids=[world["parent"]], brand_ids=[world["brand"]], routes=["/Firsat-Urunleri/"])
    assert saved["tiers"]["WHOLESALE"] == {
        "category_ids": [world["parent"]], "brand_ids": [world["brand"]], "routes": ["/firsat-urunleri"],
    }
    firm = await _firm(client, db_session, segments, SegmentType.WHOLESALE)
    ids = [str(world[k].id) for k in ("in_child", "of_brand", "free")]

    hidden_for_firm = await client.get("/api/v1/products", params={"ids": ids}, headers=firm)
    assert _ids(hidden_for_firm.json()) == {str(world["free"].id)}
    # Retail, a guest and the admin see all three.
    for headers in (auth_header(customer_user), {}, auth_header(admin_user)):
        assert _ids((await client.get("/api/v1/products", params={"ids": ids}, headers=headers)).json()) == set(ids)

    # The category (by its child) and the brand page: a notice, nothing of what is in them.
    for params in ({"category": world["child_slug"]}, {"brand": world["brand_slug"]}):
        for path in ("/api/v1/products", "/api/v1/products/summary"):
            resp = await client.get(path, params=params, headers=firm)
            assert resp.status_code == 403 and RESTRICTED in resp.json()["detail"]["message"], resp.text
    # A category it may see still lists, without the hidden product filed in it too.
    listed = await client.get("/api/v1/products", params={"category": world["other_slug"]}, headers=firm)
    assert _ids(listed.json()) == {str(world["free"].id)}

    # The product pages.
    for key in ("in_child", "of_brand"):
        resp = await client.get(f"/api/v1/products/{world[key].slug}", headers=firm)
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "ACCESS_RESTRICTED"
        assert (await client.get(f"/api/v1/products/{world[key].slug}", headers=auth_header(customer_user))).status_code == 200
    assert (await client.get(f"/api/v1/products/{world['free'].slug}", headers=firm)).status_code == 200

    # Search and its suggestions.
    found = await client.get("/api/v1/products", params={"q": world["s"]}, headers=firm)
    assert _ids(found.json()) == {str(world["free"].id)}
    suggested = (await client.get("/api/v1/products/suggest", params={"q": world["s"]}, headers=firm)).json()
    assert {p["id"] for p in suggested["products"]} == {str(world["free"].id)}
    assert not {c["slug"] for c in suggested["categories"]} & {world["child_slug"], f"gizli-{world['s']}"}

    # The menu's categories and brands.
    slugs = {c["slug"] for c in (await client.get("/api/v1/categories", headers=firm)).json()}
    assert world["child_slug"] not in slugs and f"gizli-{world['s']}" not in slugs and world["other_slug"] in slugs
    assert (await client.get(f"/api/v1/categories/by-slug/{world['child_slug']}", headers=firm)).status_code == 403
    assert world["brand_slug"] not in {b["slug"] for b in (await client.get("/api/v1/brands", headers=firm)).json()}
    assert world["brand_slug"] in {b["slug"] for b in (await client.get("/api/v1/brands")).json()}

    # The price catalogue a firm prints for its customers.
    catalogue = await client.post("/api/v1/products/price-list", headers=firm, json={"product_ids": ids, "groups": ["CAFE"]})
    assert catalogue.status_code == 200, catalogue.text
    assert {row["product_id"] for row in catalogue.json()} == {str(world["free"].id)}

    # Buying it anyway, by its variant id.
    resp = await client.post(
        "/api/v1/orders/calculate-cart", headers=firm,
        json={"items": [{"variant_id": str(world["variants"][world["in_child"].id]), "quantity": 1}],
              "payment_method": "BANK_TRANSFER", "shipping_method": "YURTICI_DOOR"},
    )
    assert resp.status_code == 422 and RESTRICTED in resp.json()["detail"]
    ok = await _quote(client, firm, [(type("V", (), {"id": world["variants"][world["free"].id]}), 1)])
    assert ok["items"]


async def test_the_storefronts_page_check(client: AsyncClient, db_session: AsyncSession, segments, world, rules, customer_user: User):
    await rules("CAFE", category_ids=[world["parent"]], brand_ids=[world["brand"]], routes=["/firsat-urunleri"])
    firm = await _firm(client, db_session, segments, SegmentType.CAFE)

    async def allowed(path: str, headers: dict) -> bool:
        resp = await client.get("/api/v1/access/check", params={"path": path}, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["allowed"]

    for path in ("/firsat-urunleri", "/en/firsat-urunleri/", "/firsat-urunleri/sayfa-2", f"/{world['child_slug']}",
                 f"/{world['brand_slug']}", f"/marka/{world['brand_slug']}", f"/{world['in_child'].slug}", f"/tr/{world['of_brand'].slug}"):
        assert not await allowed(path, firm), path
        assert await allowed(path, auth_header(customer_user)), path
    for path in ("/", f"/{world['free'].slug}", f"/{world['other_slug']}", "/firsat-urunleri-eski"):
        assert await allowed(path, firm), path


async def test_a_guests_rules_reach_the_anonymous_pages(client: AsyncClient, world, rules, customer_user: User):
    await rules("GUEST", category_ids=[world["parent"]], routes=["/fiyat-katalogu"])
    assert (await client.get("/api/v1/access/guest")).json() == {"routes": ["/fiyat-katalogu"], "catalog": True}
    assert not (await client.get("/api/v1/access/check", params={"path": f"/{world['in_child'].slug}"})).json()["allowed"]
    assert (await client.get(f"/api/v1/products/{world['in_child'].slug}")).status_code == 403
    assert (await client.get("/api/v1/products/sultanpuan")).status_code == 200
    # Signed in as retail: not a guest.
    assert (await client.get(f"/api/v1/products/{world['in_child'].slug}", headers=auth_header(customer_user))).status_code == 200


async def test_only_admins_edit_rules_and_the_home_page_cannot_be_closed(
    client: AsyncClient, customer_user: User, admin_user: User, rules
):
    assert (await client.get(ADMIN, headers=auth_header(customer_user))).status_code == 403
    assert (await client.put(f"{ADMIN}/RETAIL", headers=auth_header(admin_user), json={"routes": ["/tr/"]})).status_code == 422
    listed = (await client.get(ADMIN, headers=auth_header(admin_user))).json()
    assert set(listed["tiers"]) == {"GUEST", "RETAIL", "CAFE", "WHOLESALE", "DEALER"}
    assert isinstance(listed["categories"], list) and isinstance(listed["brands"], list)
