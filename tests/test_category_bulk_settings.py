""""Tüm Ürünlere Toplu Ayar Uygula": a category's products get the same "Genel
Kurallar" and catalogue "KDV gösterimi" in one write."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product, ProductCategory
from app.models.user import User
from tests.conftest import auth_header


@pytest.fixture
async def tree(db_session: AsyncSession):
    """A group with a subcategory: one product in each, one listed in the group
    as a second category only, one deleted, and one outside both."""
    tag = uuid.uuid4().hex[:8]
    group = Category(name=f"Grup {tag}", slug=f"grup-{tag}")
    db_session.add(group)
    await db_session.flush()
    child = Category(name=f"Alt {tag}", slug=f"alt-{tag}", parent_id=group.id)
    elsewhere = Category(name=f"Baska {tag}", slug=f"baska-{tag}")
    db_session.add_all([child, elsewhere])
    await db_session.flush()

    def product(name: str, category: Category, **fields) -> Product:
        p = Product(base_code=f"{name[:3].upper()}-{tag}", name=f"{name} {tag}", category_id=category.id, **fields)
        db_session.add(p)
        return p

    own = product("Kendi", group, erp_display_bayi=True)
    sub = product("Altta", child, free_shipping_min_quantity=5)
    linked = product("Bagli", elsewhere)
    gone = product("Silinmis", group, is_deleted=True, is_published=False)
    outside = product("Disarida", elsewhere)
    await db_session.flush()
    db_session.add(ProductCategory(product_id=linked.id, category_id=group.id, position=1))
    # Plain ids: the rows expire on commit, and the tests read them afresh.
    ids = {
        "group": group.id, "child": child.id, "elsewhere": elsewhere.id,
        "own": own.id, "sub": sub.id, "linked": linked.id, "gone": gone.id, "outside": outside.id,
    }
    await db_session.commit()

    yield ids

    await db_session.rollback()
    products = [ids[k] for k in ("own", "sub", "linked", "gone", "outside")]
    await db_session.execute(delete(ProductCategory).where(ProductCategory.product_id.in_(products)))
    await db_session.execute(delete(Product).where(Product.id.in_(products)))
    await db_session.execute(delete(Category).where(Category.id.in_([ids["child"], ids["group"], ids["elsewhere"]])))
    await db_session.commit()


async def _fresh(db: AsyncSession, product_id) -> Product:
    db.expire_all()
    return (await db.execute(select(Product).where(Product.id == product_id))).scalar_one()


def _url(category_id: int) -> str:
    return f"/api/v1/admin/categories/{category_id}/bulk-update-settings"


async def test_scope_counts_the_category_and_its_branch(client: AsyncClient, admin_user: User, tree):
    resp = await client.get(_url(tree["group"]), headers=auth_header(admin_user))
    assert resp.status_code == 200
    # Own + linked as a second category; the subcategory's adds one; the deleted one never counts.
    assert resp.json() == {"products": 2, "with_subcategories": 3}


async def test_writes_only_the_fields_sent_on_every_product(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, tree
):
    resp = await client.post(
        _url(tree["group"]),
        headers=auth_header(admin_user),
        json={"gives_points": False, "is_free_shipping": True, "erp_display_toptan": True, "erp_display_bayi": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 3}

    for key in ("own", "sub", "linked"):
        p = await _fresh(db_session, tree[key])
        assert (p.gives_points, p.is_free_shipping, p.erp_display_toptan, p.erp_display_bayi) == (False, True, True, None)
        # Not sent: each keeps its own.
        assert p.can_use_points is True and p.erp_display_kafe is None
    assert (await _fresh(db_session, tree["sub"])).free_shipping_min_quantity == 5

    for key in ("gone", "outside"):
        p = await _fresh(db_session, tree[key])
        assert (p.gives_points, p.is_free_shipping, p.erp_display_toptan) == (True, False, None)


async def test_the_category_alone_leaves_its_subcategories(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, tree
):
    admin = auth_header(admin_user)  # before the rows are read afresh, which expires the user too
    resp = await client.post(
        _url(tree["group"]),
        headers=admin,
        json={"include_subcategories": False, "free_shipping_min_quantity": 10, "erp_display_perakende": False},
    )
    assert resp.json() == {"updated": 2}
    assert (await _fresh(db_session, tree["own"])).free_shipping_min_quantity == 10
    sub = await _fresh(db_session, tree["sub"])
    assert (sub.free_shipping_min_quantity, sub.erp_display_perakende) == (5, None)

    # Null drops the condition.
    await client.post(_url(tree["group"]), headers=admin, json={"free_shipping_min_quantity": None})
    assert (await _fresh(db_session, tree["own"])).free_shipping_min_quantity is None


async def test_refuses_an_empty_or_unclear_request(client: AsyncClient, admin_user: User, customer_user: User, tree):
    url = _url(tree["group"])
    empty = await client.post(url, headers=auth_header(admin_user), json={"include_subcategories": True})
    assert empty.status_code == 422 and "Uygulanacak bir ayar" in empty.text
    # A switch is yes or no; null is not a third answer.
    assert (await client.post(url, headers=auth_header(admin_user), json={"gives_points": None})).status_code == 422
    # A field the sheet does not have is refused, not ignored.
    assert (await client.post(url, headers=auth_header(admin_user), json={"genel_kurallar": "x"})).status_code == 422
    assert (await client.post(url, headers=auth_header(customer_user), json={"gives_points": True})).status_code == 403
    missing = "/api/v1/admin/categories/999999999/bulk-update-settings"
    assert (await client.post(missing, headers=auth_header(admin_user), json={"gives_points": True})).status_code == 404
