"""GET /integrations/price-intel/catalog: the price-intelligence service's
read-only feed, and the service accounts that open it.

Covers:
- Only a local call with a live token holding price:read gets in: anything
  through the proxy (forwarding headers) or from another address is told the
  route does not exist; no token or a revoked one is 401; a missing scope 403
- Rotating a token ends the old one at once; use is recorded
- The feed leaves out the shop's own brands and deleted products, keeps
  unbranded ones, and pages through everything by id
- Each product carries its colours' stock, cost and Perakende price (the TRY
  row when there is one), its category path, its last applied Cetvel row,
  and paid sales per window (quotes and unpaid orders left out)
- The day's TCMB rates and the havale ladder come with every page
"""

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.main import app
from app.models.brand import Brand
from app.models.catalog import Category, CurrencyType, Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.pricing import PricingRecord
from app.models.service_account import PRICE_READ, ServiceAccount
from app.models.user import CustomerSegment, SegmentType, User
from app.services import exchange_rates
from scripts import service_accounts

URL = "/api/v1/integrations/price-intel/catalog"


async def _token(db: AsyncSession, name: str | None = None) -> str:
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    return await service_accounts.run("create", name or f"radar-{uuid.uuid4().hex[:6]}", [PRICE_READ], sessions)


def _h(token: str) -> dict[str, str]:
    return {"X-Service-Token": token}


async def _everything(client: AsyncClient, token: str, **params) -> list[dict]:
    """Every page, following next_after."""
    products, after = [], None
    while True:
        query = {"limit": 1000, **params, **({"after": after} if after else {})}
        resp = await client.get(URL, headers=_h(token), params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        products += body["products"]
        after = body["next_after"]
        if after is None:
            return products


@pytest.fixture
def rates(monkeypatch):
    today = exchange_rates.Rates(bulletin_date=date(2026, 11, 20), try_per_unit={"USD": Decimal("42.5000"), "EUR": Decimal("49.1000")})
    monkeypatch.setattr(exchange_rates, "_cache", (time.monotonic(), today))
    return today


@pytest.fixture
async def shop(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment], customer_user: User, monkeypatch):
    tag = uuid.uuid4().hex[:6]
    own = Brand(name=f"Kendi {tag}", slug=f"kendi-{tag}", search_term=f"Kendi {tag}")
    other = Brand(name=f"Diger {tag}", slug=f"diger-{tag}", search_term=f"Diger {tag}")
    top = Category(name=f"Nargile {tag}", slug=f"nargile-{tag}")
    db_session.add_all([own, other, top])
    await db_session.flush()
    shelf = Category(name=f"Takımlar {tag}", slug=f"takimlar-{tag}", parent_id=top.id)
    db_session.add(shelf)
    await db_session.flush()
    monkeypatch.setattr(settings, "PRICE_INTEL_EXCLUDED_BRAND_SLUGS", [own.slug, "no-such-brand"])

    def product(code: str, **kw) -> Product:
        return Product(base_code=f"{code}-{tag}", name=f"{code} {tag}", slug=f"{code.lower()}-{tag}", tags=[], **kw)

    tracked = product("PI1", brand=other.name, category_id=shelf.id, image_url="https://cdn.test/pi1.webp")
    usd = product("PI2", brand=other.name)
    unbranded = product("PI3")
    ours = product("PI4", brand=own.name)
    gone = product("PI5", brand=other.name, is_deleted=True, is_published=False)
    products = [tracked, usd, unbranded, ours, gone]
    db_session.add_all(products)
    await db_session.flush()

    def variant(p: Product, sku: str, position: int, **kw) -> ProductVariant:
        return ProductVariant(product_id=p.id, sku=f"{sku}-{tag}", position=position, **kw)

    black = variant(tracked, "PI1-SY", 0, color_name="Siyah", cost_price=Decimal("50.5000"), cost_currency=CurrencyType.USD)
    gold = variant(tracked, "PI1-GD", 1, color_name="Gold")
    retired = variant(tracked, "PI1-OLD", 2, is_active=False)
    usd_v = variant(usd, "PI2-V", 0)
    variants = [black, gold, retired, usd_v, variant(unbranded, "PI3-V", 0), variant(ours, "PI4-V", 0), variant(gone, "PI5-V", 0)]
    db_session.add_all(variants)
    await db_session.flush()

    retail, dealer = segments[SegmentType.RETAIL].id, segments[SegmentType.DEALER].id
    vat = Decimal("20.00")
    db_session.add_all([
        # Black: a TRY row and an old USD one; checkout charges the TRY one.
        VariantPrice(variant_id=black.id, segment_id=retail, currency=CurrencyType.USD,
                     price_excl_tax=Decimal("70.00"), price_incl_tax=Decimal("84.00"), vat_rate=vat),
        VariantPrice(variant_id=black.id, segment_id=retail, currency=CurrencyType.TRY,
                     price_excl_tax=Decimal("2500.00"), price_incl_tax=Decimal("3000.00"), vat_rate=vat),
        VariantPrice(variant_id=black.id, segment_id=dealer, currency=CurrencyType.TRY,
                     price_excl_tax=Decimal("1800.00"), price_incl_tax=Decimal("2160.00"), vat_rate=vat),
        VariantPrice(variant_id=gold.id, segment_id=retail, currency=CurrencyType.TRY,
                     price_excl_tax=Decimal("2500.00"), price_incl_tax=Decimal("3000.00"), vat_rate=vat),
        VariantPrice(variant_id=usd_v.id, segment_id=retail, currency=CurrencyType.USD,
                     price_excl_tax=Decimal("50.00"), price_incl_tax=Decimal("60.00"), vat_rate=vat),
        Inventory(variant_id=black.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=4),
        Inventory(variant_id=black.id, warehouse_type=WarehouseType.DEALER_STOCK, quantity=99),
    ])

    now = datetime.now(timezone.utc)
    orders = []

    def sell(v: ProductVariant, qty: int, days_ago: int, *, segment=SegmentType.RETAIL,
             status=OrderStatus.APPROVED, is_quote=False) -> None:
        order = Order(
            order_number=f"PI-{uuid.uuid4().hex[:10]}", user_id=customer_user.id,
            total_amount=Decimal("1"), shipping_cost=Decimal("0"), discount_amount=Decimal("0"),
            payment_method=PaymentMethod.QUOTE if is_quote else PaymentMethod.BANK_TRANSFER,
            status=status, is_quote=is_quote, segment_code=segment, paid_at=now - timedelta(days=days_ago),
        )
        order.items = [OrderItem(variant_id=v.id, quantity=qty, unit_price=Decimal("1000"), line_total=Decimal(1000 * qty))]
        orders.append(order)

    sell(black, 2, 1)
    sell(gold, 1, 3, status=OrderStatus.SHIPPED)
    sell(black, 5, 20, segment=SegmentType.DEALER)
    sell(gold, 1, 60, status=OrderStatus.DELIVERED)
    sell(black, 7, 200)  # outside every window
    sell(black, 9, 1, status=OrderStatus.AWAITING_TRANSFER)  # not paid
    sell(black, 9, 1, is_quote=True)  # a quote
    db_session.add_all(orders)

    sheet = {"retail": {"price": "3000.00"}}
    older = PricingRecord(product_code=tracked.base_code, product_name=tracked.name, sheet=sheet,
                          applied_at=now - timedelta(days=9), applied_summary={"product_id": str(tracked.id)})
    latest = PricingRecord(product_code=tracked.base_code, product_name=tracked.name, sheet=sheet,
                           applied_at=now - timedelta(days=2), applied_summary={"product_id": str(tracked.id)})
    draft = PricingRecord(product_code=tracked.base_code, product_name=tracked.name, sheet=sheet)
    db_session.add_all([older, latest, draft])
    await db_session.commit()

    ids = [p.id for p in products]
    vids = [v.id for v in variants]
    order_ids = [o.id for o in orders]
    record_ids = [older.id, latest.id, draft.id]
    category_ids, brand_ids = [shelf.id, top.id], [own.id, other.id]

    yield {"tag": tag, "tracked": tracked, "usd": usd, "unbranded": unbranded, "ours": ours, "gone": gone,
           "black": black, "gold": gold, "other": other, "shelf": shelf, "top": top, "latest": latest}

    await db_session.rollback()
    await db_session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
    await db_session.execute(delete(Order).where(Order.id.in_(order_ids)))
    await db_session.execute(delete(PricingRecord).where(PricingRecord.id.in_(record_ids)))
    await db_session.execute(delete(Inventory).where(Inventory.variant_id.in_(vids)))
    await db_session.execute(delete(VariantPrice).where(VariantPrice.variant_id.in_(vids)))
    await db_session.execute(delete(ProductVariant).where(ProductVariant.id.in_(vids)))
    await db_session.execute(delete(Product).where(Product.id.in_(ids)))
    await db_session.execute(delete(Category).where(Category.id.in_(category_ids)))
    await db_session.execute(delete(Brand).where(Brand.id.in_(brand_ids)))
    await db_session.commit()


# ── Who gets in ──────────────────────────────────────────────────────────────


async def test_a_local_call_with_a_live_token_gets_in(client: AsyncClient, db_session: AsyncSession, rates):
    token = await _token(db_session)
    resp = await client.get(URL, headers=_h(token), params={"limit": 1})
    assert resp.status_code == 200, resp.text
    account = (await db_session.execute(
        ServiceAccount.__table__.select().where(ServiceAccount.token_prefix == token[:8])
    )).one()
    assert account.last_used_at is not None
    assert len(account.token_hash) == 64 and token not in account.token_hash  # only its hash is kept


async def test_without_a_token_or_with_a_wrong_one_it_is_refused(client: AsyncClient, db_session: AsyncSession):
    assert (await client.get(URL)).status_code == 401
    assert (await client.get(URL, headers=_h("nsa_" + "x" * 43))).status_code == 401
    assert (await client.get(URL, headers=_h("Bearer something"))).status_code == 401


async def test_anything_through_the_proxy_or_from_elsewhere_finds_nothing(client: AsyncClient, db_session: AsyncSession):
    token = await _token(db_session)
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
        resp = await client.get(URL, headers={**_h(token), header: "127.0.0.1"})
        assert resp.status_code == 404, header
    # A request from the internet, as uvicorn sees it behind Caddy (--proxy-headers).
    async with AsyncClient(transport=ASGITransport(app=app, client=("203.0.113.7", 50000)), base_url="http://test") as outside:
        assert (await outside.get(URL, headers=_h(token))).status_code == 404


async def test_a_revoked_or_rotated_token_no_longer_works(client: AsyncClient, db_session: AsyncSession, rates):
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    name = f"radar-{uuid.uuid4().hex[:6]}"
    first = await _token(db_session, name)
    second = await service_accounts.run("rotate", name, [], sessions)
    assert (await client.get(URL, headers=_h(first), params={"limit": 1})).status_code == 401
    assert (await client.get(URL, headers=_h(second), params={"limit": 1})).status_code == 200

    await service_accounts.run("revoke", name, [], sessions)
    assert (await client.get(URL, headers=_h(second), params={"limit": 1})).status_code == 401
    with pytest.raises(SystemExit):
        await service_accounts.run("rotate", name, [], sessions)  # revoked for good
    with pytest.raises(SystemExit):
        await service_accounts.run("create", name, [PRICE_READ], sessions)  # its name stays taken


async def test_an_account_without_the_scope_is_forbidden(client: AsyncClient, db_session: AsyncSession):
    from app.api.service_auth import new_token, token_hash

    token = new_token()
    db_session.add(ServiceAccount(name=f"bare-{uuid.uuid4().hex[:6]}", token_hash=token_hash(token), token_prefix=token[:8], scopes=[]))
    await db_session.commit()
    assert (await client.get(URL, headers=_h(token))).status_code == 403


async def test_only_known_scopes_can_be_given(db_session: AsyncSession):
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    with pytest.raises(SystemExit):
        await service_accounts.run("create", f"x-{uuid.uuid4().hex[:6]}", ["price:write"], sessions)
    with pytest.raises(SystemExit):
        await service_accounts.run("create", f"x-{uuid.uuid4().hex[:6]}", [], sessions)


# ── What it says ─────────────────────────────────────────────────────────────


async def test_own_brands_and_deleted_products_are_left_out(client: AsyncClient, db_session: AsyncSession, shop, rates):
    token = await _token(db_session)
    ids = {p["id"] for p in await _everything(client, token)}
    assert {str(shop[k].id) for k in ("tracked", "usd", "unbranded")} <= ids
    assert str(shop["ours"].id) not in ids and str(shop["gone"].id) not in ids


async def test_a_product_carries_its_stock_cost_prices_cetvel_and_sales(
    client: AsyncClient, db_session: AsyncSession, shop, rates
):
    token = await _token(db_session)
    body = (await client.get(URL, headers=_h(token), params={"limit": 1})).json()
    assert body["rates"] == {"bulletin_date": "2026-11-20", "try_per_unit": {"USD": "42.5000", "EUR": "49.1000"}}
    assert body["havale_tiers"] and all({"threshold", "rate"} <= set(t) for t in body["havale_tiers"])
    assert shop["ours"].brand not in str(body["excluded_brand_slugs"])  # slugs, not names
    assert f"kendi-{shop['tag']}" in body["excluded_brand_slugs"]

    products = {p["id"]: p for p in await _everything(client, token)}
    p = products[str(shop["tracked"].id)]
    assert p["code"] == f"PI1-{shop['tag']}" and p["slug"] == f"pi1-{shop['tag']}"
    assert p["brand_id"] == shop["other"].id and p["brand_slug"] == f"diger-{shop['tag']}"
    assert p["category"]["id"] == shop["shelf"].id
    assert p["category_path"] == [f"Nargile {shop['tag']}", f"Takımlar {shop['tag']}"]
    assert p["image_url"] == "https://cdn.test/pi1.webp" and p["is_published"] is True

    # Active colours only, in their order; the TRY row stands over the old USD one.
    assert [v["color_name"] for v in p["variants"]] == ["Siyah", "Gold"]
    black, gold = p["variants"]
    assert black["stock"] == 4 and gold["stock"] == 0  # the central stock only
    assert black["cost_price"] == "50.5000" and black["cost_currency"] == "USD"
    assert gold["cost_price"] is None
    assert black["retail"] == {"currency": "TRY", "price_excl_tax": "2500.00", "price_incl_tax": "3000.00", "vat_rate": "20.00"}
    assert p["retail"] == black["retail"] and p["retail_uniform"] is True

    assert p["cetvel"]["id"] == str(shop["latest"].id)  # the latest applied row, not the draft

    sales = p["sales"]
    assert sales["d7"] == {"quantity": 3, "retail_quantity": 3, "revenue": "3000.00"}
    assert sales["d30"] == {"quantity": 8, "retail_quantity": 3, "revenue": "8000.00"}
    assert sales["d90"]["quantity"] == 9

    usd = products[str(shop["usd"].id)]
    assert usd["retail"]["currency"] == "USD" and usd["retail"]["price_incl_tax"] == "60.00"
    assert usd["cetvel"] is None
    assert usd["sales"]["d90"] == {"quantity": 0, "retail_quantity": 0, "revenue": "0.00"}
    assert products[str(shop["unbranded"].id)]["brand_id"] is None


async def test_pages_follow_one_another_by_id(client: AsyncClient, db_session: AsyncSession, shop, rates):
    token = await _token(db_session)
    first = (await client.get(URL, headers=_h(token), params={"limit": 2})).json()
    assert len(first["products"]) == 2 and first["next_after"] == first["products"][1]["id"]
    second = (await client.get(URL, headers=_h(token), params={"limit": 2, "after": first["next_after"]})).json()
    assert second["products"][0]["id"] > first["products"][1]["id"]
    everything = await _everything(client, token, limit=3)
    assert len({p["id"] for p in everything}) == len(everything)  # no product twice
    assert (await client.get(URL, headers=_h(token), params={"limit": 1001})).status_code == 422


async def test_no_rates_is_said_not_guessed(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    async def down():
        raise exchange_rates.RatesUnavailable("TCMB down")

    from app.api.v1 import integrations

    monkeypatch.setattr(integrations, "get_rates", down)
    token = await _token(db_session)
    body = (await client.get(URL, headers=_h(token), params={"limit": 1})).json()
    assert body["rates"] is None
