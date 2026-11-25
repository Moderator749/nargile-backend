"""Three things on one customer: access rules of their own ("Cari bazında")
on top of their group's, Sultan Puan / Sultan Kumbara for bireysel Perakende
accounts only, and their purchase history in the panel."""

from datetime import date, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus
from app.models.user import SegmentType, User, UserRole
from tests.conftest import ADDRESS, CONSENTS, auth_header
from tests.test_b2b_havale import _firm
from tests.test_tier_access import RESTRICTED, rules, world  # noqa: F401  (fixtures)

CUSTOMER_RULES = "/api/v1/admin/access-rules/customers"


async def _second_customer(db: AsyncSession, segments) -> User:
    import uuid

    user = User(
        email=f"customer2-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x", role=UserRole.CUSTOMER,
        segment_id=segments[SegmentType.RETAIL].id, is_active=True, is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ── Cari bazında ─────────────────────────────────────────────────────────────


async def test_a_customers_own_rules_hide_from_them_only_on_top_of_their_group(
    client: AsyncClient, db_session: AsyncSession, segments, world, rules, admin_user: User, customer_user: User  # noqa: F811
):
    other = await _second_customer(db_session, segments)
    h = auth_header(admin_user)
    ids = [str(world[k].id) for k in ("in_child", "of_brand", "free")]
    # Their group (Perakende) hides the brand; the customer alone, the category and a page.
    await rules("RETAIL", brand_ids=[world["brand"]])
    saved = await client.put(
        f"{CUSTOMER_RULES}/{customer_user.id}", headers=h,
        json={"category_ids": [world["parent"]], "routes": ["/Firsat-Urunleri"]},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["rules"] == {"category_ids": [world["parent"]], "brand_ids": [], "routes": ["/firsat-urunleri"]}
    assert body["customer"]["rule_count"] == 2

    mine = await client.get("/api/v1/products", params={"ids": ids}, headers=auth_header(customer_user))
    assert {p["id"] for p in mine.json()} == {str(world["free"].id)}  # union: brand (group) + category (own)
    theirs = await client.get("/api/v1/products", params={"ids": ids}, headers=auth_header(other))
    assert {p["id"] for p in theirs.json()} == {str(world["in_child"].id), str(world["free"].id)}  # group only

    resp = await client.get(f"/api/v1/products/{world['in_child'].slug}", headers=auth_header(customer_user))
    assert resp.status_code == 403 and RESTRICTED in resp.json()["detail"]["message"]
    check = await client.get("/api/v1/access/check", params={"path": "/firsat-urunleri"}, headers=auth_header(customer_user))
    assert check.json()["allowed"] is False
    check = await client.get("/api/v1/access/check", params={"path": "/firsat-urunleri"}, headers=auth_header(other))
    assert check.json()["allowed"] is True

    listed = (await client.get(CUSTOMER_RULES, headers=h)).json()
    assert any(c["id"] == str(customer_user.id) and c["rule_count"] == 2 for c in listed)
    read = (await client.get(f"{CUSTOMER_RULES}/{customer_user.id}", headers=h)).json()
    assert read["rules"]["category_ids"] == [world["parent"]]

    # Cleared: they see what their group sees again.
    await client.put(f"{CUSTOMER_RULES}/{customer_user.id}", headers=h, json={})
    again = await client.get("/api/v1/products", params={"ids": ids}, headers=auth_header(customer_user))
    assert {p["id"] for p in again.json()} == {str(world["in_child"].id), str(world["free"].id)}


async def test_a_customers_rules_reach_their_cart_and_catalogue(
    client: AsyncClient, db_session: AsyncSession, segments, world, rules, admin_user: User  # noqa: F811
):
    firm = await _firm(client, db_session, segments, SegmentType.DEALER)
    me = (await client.get("/api/v1/auth/me", headers=firm)).json()
    await client.put(f"{CUSTOMER_RULES}/{me['id']}", headers=auth_header(admin_user), json={"brand_ids": [world["brand"]]})
    resp = await client.post(
        "/api/v1/orders/calculate-cart", headers=firm,
        json={"items": [{"variant_id": str(world["variants"][world["of_brand"].id]), "quantity": 1}],
              "payment_method": "BANK_TRANSFER", "shipping_method": "YURTICI_DOOR"},
    )
    assert resp.status_code == 422 and RESTRICTED in resp.json()["detail"]
    ids = [str(world[k].id) for k in ("of_brand", "free")]
    catalogue = await client.post("/api/v1/products/price-list", headers=firm, json={"product_ids": ids, "groups": ["CAFE"]})
    assert {row["product_id"] for row in catalogue.json()} == {str(world["free"].id)}
    await client.put(f"{CUSTOMER_RULES}/{me['id']}", headers=auth_header(admin_user), json={})


async def test_customer_rules_are_admin_only_and_need_a_customer(client: AsyncClient, admin_user: User, customer_user: User):
    import uuid

    assert (await client.get(CUSTOMER_RULES, headers=auth_header(customer_user))).status_code == 403
    assert (await client.put(f"{CUSTOMER_RULES}/{uuid.uuid4()}", headers=auth_header(admin_user), json={})).status_code == 404


async def test_the_customer_search_finds_a_phone_by_its_digits(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User
):
    await db_session.execute(update(User).where(User.id == customer_user.id).values(phone="0532 987 65 43"))
    await db_session.commit()
    found = await client.get("/api/v1/admin/customers", headers=auth_header(admin_user), params={"q": "5329876543"})
    assert str(customer_user.id) in {c["id"] for c in found.json()}


# ── Sultan Puan / Sultan Kumbara: bireysel Perakende only ────────────────────


async def test_b2b_accounts_have_no_wallet_and_no_points(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User, customer_user: User
):
    retail = (await client.get("/api/v1/me/wallet", headers=auth_header(customer_user))).json()
    assert retail["eligible"] is True
    for segment in (SegmentType.CAFE, SegmentType.WHOLESALE, SegmentType.DEALER):
        firm = await _firm(client, db_session, segments, segment)
        wallet = (await client.get("/api/v1/me/wallet", headers=firm)).json()
        assert wallet["eligible"] is False, segment
        me = (await client.get("/api/v1/auth/me", headers=firm)).json()
        h = auth_header(admin_user)
        detail = (await client.get(f"/api/v1/admin/customers/{me['id']}", headers=h)).json()
        assert detail["loyalty_eligible"] is False and detail["loyalty_history"] is False
        refused = await client.post(f"/api/v1/admin/customers/{me['id']}/points", headers=h, json={"amount": 500, "note": "deneme"})
        assert refused.status_code == 422 and "Perakende" in refused.json()["detail"]

    # A bireysel account put on Kafe loses them too, and its old points stay readable.
    h = auth_header(admin_user)
    given = await client.post(f"/api/v1/admin/customers/{customer_user.id}/points", headers=h, json={"amount": 100, "note": "eski puan"})
    assert given.status_code == 200, given.text
    await client.put(f"/api/v1/admin/customers/{customer_user.id}/segment", headers=h, json={"segment": "CAFE"})
    detail = (await client.get(f"/api/v1/admin/customers/{customer_user.id}", headers=h)).json()
    assert detail["loyalty_eligible"] is False and detail["loyalty_history"] is True
    history = (await client.get(f"/api/v1/admin/customers/{customer_user.id}/wallet", headers=h)).json()
    assert history["eligible"] is False and history["history"]


async def test_a_b2b_cart_earns_and_spends_no_points(client: AsyncClient, db_session: AsyncSession, segments, catalog):
    normal_v, _ = catalog
    firm = await _firm(client, db_session, segments, SegmentType.CAFE)
    from app.models.catalog import VariantPrice

    db_session.add(VariantPrice(
        variant_id=normal_v.id, segment_id=segments[SegmentType.CAFE].id, currency="TRY",
        price_excl_tax=Decimal("100.00"), price_incl_tax=Decimal("120.00"), vat_rate=Decimal("20.00"),
    ))
    await db_session.commit()
    quote = await client.post(
        "/api/v1/orders/calculate-cart", headers=firm,
        json={"items": [{"variant_id": str(normal_v.id), "quantity": 1}], "payment_method": "BANK_TRANSFER", "shipping_method": "YURTICI_DOOR"},
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["loyalty_points_to_earn"] == 0 and quote.json()["perks_eligible"] is False


# ── Sipariş / Alışveriş Geçmişi ──────────────────────────────────────────────


async def _order(client: AsyncClient, user: User, variant_id, quantity: int) -> dict:
    resp = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user),
        json={**CONSENTS, "payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(variant_id), "quantity": quantity}],
              "shipping_address": ADDRESS, "shipping_consent": True},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_a_customers_orders_newest_first_with_lines_and_totals(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, heavy_v = catalog
    first = await _order(client, customer_user, normal_v.id, 1)
    second = await _order(client, customer_user, heavy_v.id, 3)
    await db_session.execute(update(Order).where(Order.order_number == first["order_number"]).values(status=OrderStatus.APPROVED))
    await db_session.commit()

    h = auth_header(admin_user)
    url = f"/api/v1/admin/customers/{customer_user.id}/orders"
    history = (await client.get(url, headers=h)).json()
    numbers = [o["order_number"] for o in history["orders"]]
    assert numbers[:2] == [second["order_number"], first["order_number"]]  # newest first
    assert history["order_count"] >= 2 and history["paid_count"] >= 1
    latest = history["orders"][0]
    assert latest["lines"] == [{"product_name": "Odun Komuru", "variant": heavy_v.sku, "sku": heavy_v.sku, "quantity": 3}]
    assert latest["status"] == "AWAITING_TRANSFER" and latest["payment_method"] == "BANK_TRANSFER" and latest["currency"] == "TRY"
    # Only what was paid counts as spent.
    assert Decimal(history["total_spent"]) == Decimal(first["total_amount"])

    today = date.today()
    none = (await client.get(url, headers=h, params={"date_from": str(today + timedelta(days=2))})).json()
    assert none["orders"] == [] and none["order_count"] == 0 and Decimal(none["total_spent"]) == 0
    ranged = (await client.get(url, headers=h, params={"date_from": str(today - timedelta(days=1)), "date_to": str(today + timedelta(days=1))})).json()
    assert ranged["order_count"] == history["order_count"]

    assert (await client.get(url, headers=auth_header(customer_user))).status_code == 403
