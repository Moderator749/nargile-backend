"""Brand names in capitals: the rule, the brand endpoints keeping a name as
sent (trimmed), and the one-off script that put the old names into capitals."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.brand_names import brand_caps
from app.models.brand import Brand
from app.models.catalog import Product
from app.models.user import User
from scripts import uppercase_brand_names as script
from tests.conftest import auth_header



@pytest.fixture
def tag() -> str:
    return str(uuid.uuid4().int)[:10]  # digits: the same in capitals


@pytest.fixture
async def cleanup(db_session: AsyncSession, tag: str):
    yield
    await db_session.rollback()
    await db_session.execute(delete(Product).where(Product.base_code.contains(tag)))
    await db_session.execute(delete(Brand).where(Brand.slug.contains(tag)))
    await db_session.commit()


def test_brand_caps_is_the_neutral_rule():
    assert brand_caps("Shisha Foil") == "SHISHA FOIL"
    assert brand_caps("telamon bowl") == "TELAMON BOWL"
    assert brand_caps("mr. wood") == "MR. WOOD"
    assert brand_caps("İSTANBUL ğüşöç ılık") == "İSTANBUL ĞÜŞÖÇ ILIK"
    assert brand_caps("straße") == "STRAßE"  # the same length: no "SS"
    assert brand_caps("ALREADY 123") == "ALREADY 123"


async def test_brand_endpoints_trim_but_keep_the_case(client: AsyncClient, admin_user: User, tag: str, cleanup):
    h = auth_header(admin_user)
    created = await client.post("/api/v1/admin/brands", headers=h, json={"name": f"  SHISHA Foil {tag}  "})
    assert created.status_code == 201, created.text
    brand = created.json()
    assert brand["name"] == f"SHISHA Foil {tag}"
    assert brand["search_term"] == f"SHISHA Foil {tag}"

    updated = await client.put(f"/api/v1/admin/brands/{brand['id']}", headers=h, json={"name": f"\tİZMİR iz {tag} "})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == f"İZMİR iz {tag}"

    blank = await client.post("/api/v1/admin/brands", headers=h, json={"name": "   "})
    assert blank.status_code == 422


async def _brand(db: AsyncSession, name: str, slug: str, **kw) -> Brand:
    b = Brand(name=name, slug=slug, search_term=name, is_active=kw.pop("is_active", True), sort_order=kw.pop("sort_order", 0), **kw)
    db.add(b)
    await db.flush()
    return b


async def _product(db: AsyncSession, tag: str, brand: str | None) -> Product:
    p = Product(base_code=f"BC-{tag}-{uuid.uuid4().hex[:6]}", name="Brand caps test", tags=[], brand=brand)
    db.add(p)
    await db.flush()
    return p


async def test_script_puts_names_into_capitals_and_merges_twins(db_session: AsyncSession, tag: str, cleanup):
    db = db_session
    mixed = await _brand(db, f"Shisha Foil {tag}", f"shisha-foil-{tag}", sort_order=10)
    # The same name in capitals, with a logo: it stays, and takes the other's products.
    caps = await _brand(db, f"SHISHA FOIL {tag}", f"shisha-foil-2-{tag}", logo_path="brands/x.webp", sort_order=20)
    lower = await _brand(db, f"telamon bowl {tag}", f"telamon-bowl-{tag}", description="Kase.")
    done = await _brand(db, f"MR. WOOD {tag}", f"mr-wood-{tag}")
    await db.commit()
    mixed_id, caps_id, lower_id, done_id = mixed.id, caps.id, lower.id, done.id
    caps_slug, lower_slug, done_slug = caps.slug, lower.slug, done.slug

    p_mixed = await _product(db, tag, f"shisha foil {tag}")  # links to a twin (case aside)
    p_caps = await _product(db, tag, f"SHISHA FOIL {tag}")
    p_lower = await _product(db, tag, f"Telamon Bowl {tag}")
    p_free = await _product(db, tag, f"no such brand {tag}")
    p_done = await _product(db, tag, f"mr. wood {tag}")
    await db.commit()
    linked = {p.id: p.brand_id for p in (p_mixed, p_caps, p_lower, p_free, p_done)}
    ids = {name: p.id for name, p in {"mixed": p_mixed, "caps": p_caps, "lower": p_lower, "free": p_free, "done": p_done}.items()}
    assert linked[ids['mixed']] == mixed_id and linked[ids['caps']] in {mixed_id, caps_id}
    assert linked[ids['free']] is None

    # A dry run plans it and writes nothing.
    plan = await script.run(db, apply=False, scope=tag)
    await db.rollback()
    assert {(r["before"], r["after"]) for r in plan.renamed} == {(f"telamon bowl {tag}", f"TELAMON BOWL {tag}")}
    assert [(m["keep"]["id"], [d["id"] for d in m["drop"]]) for m in plan.merged] == [(caps_id, [mixed_id])]
    assert await db.scalar(select(Brand.name).where(Brand.id == lower_id)) == f"telamon bowl {tag}"

    plan = await script.run(db, apply=True, scope=tag)
    await db.commit()
    db.expire_all()

    rows = {b.slug: b for b in (await db.scalars(select(Brand).where(Brand.slug.contains(tag)))).all()}
    assert set(rows) == {caps_slug, lower_slug, done_slug}  # the mixed-case twin is gone
    assert rows[caps_slug].name == f"SHISHA FOIL {tag}" and rows[caps_slug].logo_path == "brands/x.webp"
    assert rows[lower_slug].name == f"TELAMON BOWL {tag}" and rows[lower_slug].description == "Kase."
    assert rows[done_slug].name == f"MR. WOOD {tag}"

    products = {
        p.id: (p.brand, p.brand_id)
        for p in (await db.scalars(select(Product).where(Product.base_code.contains(tag)))).all()
    }
    assert products[ids['mixed']] == (f"SHISHA FOIL {tag}", caps_id)
    assert products[ids['caps']] == (f"SHISHA FOIL {tag}", caps_id)
    assert products[ids['lower']] == (f"TELAMON BOWL {tag}", lower_id)
    assert products[ids['done']] == (f"MR. WOOD {tag}", done_id)
    assert products[ids['free']] == (f"NO SUCH BRAND {tag}", None)

    # Idempotent: nothing left to do.
    again = await script.run(db, apply=True, scope=tag)
    await db.rollback()
    assert again.empty


async def test_script_keeper_takes_logo_and_description_it_lacks(db_session: AsyncSession, tag: str, cleanup):
    db = db_session
    # Neither has a logo; the active one stays and takes the inactive twin's description.
    active = await _brand(db, f"Kong {tag}", f"kong-{tag}")
    twin = await _brand(db, f"KONG {tag}", f"kong-bowl-{tag}", is_active=False, description="Rus kaseleri.")
    await db.commit()
    active_id, twin_id, twin_slug = active.id, twin.id, twin.slug
    plan = await script.run(db, apply=True, scope=tag)
    await db.commit()
    db.expire_all()
    assert [(m["keep"]["id"], [d["slug"] for d in m["drop"]]) for m in plan.merged] == [(active_id, [twin_slug])]
    kept = await db.get(Brand, active_id)
    assert kept.name == f"KONG {tag}" and kept.description == "Rus kaseleri." and kept.is_active
    assert await db.get(Brand, twin_id) is None
