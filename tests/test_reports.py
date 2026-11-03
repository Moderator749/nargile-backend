"""Raporlar.

One month of sales, built by hand so every figure can be worked out on paper:

  o1  Perakende, card,    paid 10 Oct  A ×2 at 1200          cost 500 frozen
  o2  Bayi,      havale,  paid 11 Oct  A ×1 at 1200, B ×1 at 600, 10% off;
                                       B's cost never frozen: today's 300
  o3  Perakende, havale,  paid 12 Oct  B ×2 at 600, brought back ("Kurtar")
  o4  card, cancelled · o5 card, failed · o6 havale, still waiting
  o7  Perakende, card,    paid 25 Sep  A ×1 at 1200 (the period before)
  o8  a paid quote: never a sale

Prices are VAT-inclusive at 20%. Each test gets a year of its own, and the
catalog-wide reports are narrowed to a brand made for it, so nothing else the
suite writes reaches these sums.

Covers:
- Revenue after discounts, VAT out for profit, margin on costed lines,
  estimated costs, Kurtarılan Ciro, and the change against the period before
- The filters: customer group, payment method, a category with its children
- Trend buckets by hour, day and week, zero-filled
- Customer group shares and margins; best sellers by product/category/brand
- Card against havale: conversion and cancellation
- Profitability with the margin threshold; list-price margins per group, as
  listed and after the havale discount, with the erosion alert (a USD cost at
  today's rate)
- Stock that has not sold in 60 days; products missing their cost
- Every table as an Excel file; admins only; impossible periods refused
"""

import itertools
import time as clock
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brand import Brand
from app.models.catalog import Category, Inventory, Product, ProductVariant, VariantPrice
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services import exchange_rates
from app.services.checkout_engine import CENTRAL_STOCK
from app.services.exchange_rates import Rates
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

TR = ZoneInfo("Europe/Istanbul")
BASE = "/api/v1/admin/reports"
D = Decimal
_years = itertools.count(1991)


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    """Today's TCMB rates, without TCMB."""
    today = Rates(bulletin_date=date.today(), try_per_unit={"USD": D("40.0000"), "EUR": D("45.0000")})
    monkeypatch.setattr(exchange_rates, "_cache", (clock.monotonic(), today))


@dataclass
class Shop:
    year: int
    parent: Category
    child: Category
    brand: Brand
    other_brand: Brand
    a: Product
    b: Product
    c: Product
    d: Product
    e: Product

    def window(self, **extra) -> dict:
        return {"from": f"{self.year}-10-01", "to": f"{self.year}-10-31", **extra}


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=TR)


@pytest.fixture
async def shop(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> Shop:
    year, tag = next(_years), uuid.uuid4().hex[:6]
    long_ago = datetime.now(timezone.utc) - timedelta(days=700)

    parent = Category(name=f"Nargile {tag}", slug=f"nargile-{tag}")
    other = Category(name=f"Kömür {tag}", slug=f"komur-{tag}")
    db_session.add_all([parent, other])
    await db_session.flush()
    child = Category(name=f"Gövde {tag}", slug=f"govde-{tag}", parent_id=parent.id)
    brand = Brand(name=f"Marka X {tag}", slug=f"marka-x-{tag}", search_term=f"x{tag}")
    other_brand = Brand(name=f"Marka Y {tag}", slug=f"marka-y-{tag}", search_term=f"y{tag}")
    db_session.add_all([child, brand, other_brand])
    await db_session.flush()

    async def product(
        code, *, category, brand, cost, currency="TRY", stock=None, prices=(), created=long_ago, published=True, **switches
    ):
        p = Product(
            base_code=f"{code}-{tag}", name=f"Ürün {code} {tag}", tags=[], category_id=category.id,
            # The brand is linked from its name, as every writer links it.
            brand=brand.name, created_at=created, is_published=published, **switches,
        )
        db_session.add(p)
        await db_session.flush()
        v = ProductVariant(
            product_id=p.id, sku=f"{code}-{tag}", cost_price=D(cost) if cost else None, cost_currency=currency,
        )
        db_session.add(v)
        await db_session.flush()
        for segment, excl in prices:
            db_session.add(VariantPrice(
                variant_id=v.id, segment_id=segments[segment].id, currency="TRY",
                price_excl_tax=D(excl), price_incl_tax=(D(excl) * D("1.2")).quantize(D("0.01")), vat_rate=D("20"),
            ))
        if stock is not None:
            db_session.add(Inventory(variant_id=v.id, warehouse_type=CENTRAL_STOCK, quantity=stock))
        p.variant = v
        return p

    R, B_ = SegmentType.RETAIL, SegmentType.DEALER
    # A gives Bayi the havale discount; the switch is off unless turned on.
    a = await product(
        "A", category=child, brand=brand, cost="500", stock=10, prices=[(R, "1000"), (B_, "550")],
        has_dealer_havale_discount=True,
    )
    b = await product("B", category=other, brand=other_brand, cost="300", prices=[(R, "500")])
    c = await product("C", category=other, brand=brand, cost="100", stock=5)
    d = await product("D", category=other, brand=brand, cost=None, stock=3,
                      created=datetime.now(timezone.utc), published=False)
    e = await product("E", category=other, brand=brand, cost="10", currency="USD", prices=[(R, "1000"), (B_, "450")])

    def buyer(segment):
        return User(
            email=f"rapor-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x", role=UserRole.CUSTOMER,
            segment_id=segments[segment].id, is_active=True,
        )

    retail, dealer = buyer(R), buyer(B_)
    db_session.add_all([retail, dealer])
    await db_session.flush()

    def order(user, segment, method, status, at, lines, *, discount="0", paid=True, quote=False, recovered=False):
        total = sum((D(price) * qty for _, qty, price, _ in lines), D(0))
        o = Order(
            order_number=f"R{uuid.uuid4().hex[:20]}", user_id=user.id, total_amount=total,
            discount_amount=D(discount), shipping_cost=D(0), payment_method=method, status=status,
            segment_code=segment, created_at=at, paid_at=at if paid else None, is_quote=quote,
            recovered_at=at if recovered else None,
            items=[
                OrderItem(
                    variant_id=p.variant.id, quantity=qty, unit_price=D(price), line_total=D(price) * qty,
                    vat_rate=D("20"), unit_cost=D(cost) if cost else None,
                )
                for p, qty, price, cost in lines
            ],
        )
        db_session.add(o)

    card, havale = PaymentMethod.CREDIT_CARD, PaymentMethod.BANK_TRANSFER
    order(retail, R, card, OrderStatus.APPROVED, _at(year, 10, 10), [(a, 2, "1200", "500")])
    order(dealer, B_, havale, OrderStatus.DELIVERED, _at(year, 10, 11), [(a, 1, "1200", "500"), (b, 1, "600", None)], discount="180")
    order(retail, R, havale, OrderStatus.APPROVED, _at(year, 10, 12), [(b, 2, "600", None)], recovered=True)
    order(retail, R, card, OrderStatus.CANCELLED, _at(year, 10, 12), [(a, 1, "1200", "500")], paid=False)
    order(retail, R, card, OrderStatus.PAYMENT_FAILED, _at(year, 10, 13), [(a, 1, "1200", "500")], paid=False)
    order(retail, R, havale, OrderStatus.AWAITING_TRANSFER, _at(year, 10, 14), [(a, 1, "1200", "500")], paid=False)
    order(retail, R, card, OrderStatus.APPROVED, _at(year, 9, 25), [(a, 1, "1200", "500")])
    order(dealer, B_, PaymentMethod.QUOTE, OrderStatus.APPROVED, _at(year, 10, 15), [(a, 5, "1200", "500")], quote=True)
    # A sold five days ago, so it is not stock that sits.
    order(dealer, B_, card, OrderStatus.APPROVED, datetime.now(timezone.utc) - timedelta(days=5), [(a, 1, "660", "500")])
    await db_session.commit()
    return Shop(year, parent, child, brand, other_brand, a, b, c, d, e)


async def _get(client: AsyncClient, admin: User, path: str, params: dict):
    resp = await client.get(f"{BASE}/{path}", headers=auth_header(admin), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Summary ──────────────────────────────────────────────────────────────────


async def test_the_months_figures_and_the_change_on_the_month_before(client, admin_user, shop):
    body = await _get(client, admin_user, "summary", shop.window())
    now, then, changes = body["current"], body["previous"], body["changes"]

    # 2400 + (1200 + 600) less 10% + 1200; the quote, the unpaid and September left out.
    assert D(now["revenue"]) == D("5220.00") and now["orders"] == 3
    assert D(now["average_order_value"]) == D("1740.00")
    # VAT out: 2000 + 900 + 450 + 1000. Cost: 1000 + 500 + 300 + 600.
    assert D(now["revenue_excl_vat"]) == D("4350.00")
    assert D(now["gross_profit"]) == D("1950.00") and D(now["margin_percent"]) == D("44.8")
    assert D(now["costed_share_percent"]) == D("100.0")
    assert now["estimated_cost_lines"] == 2  # B's two lines, at today's cost
    assert D(now["recovered_revenue"]) == D("1200.00") and now["recovered_orders"] == 1

    # The 31 days before 1 October: 31 August – 30 September.
    assert body["previous_period"] == {"date_from": f"{shop.year}-08-31", "date_to": f"{shop.year}-09-30"}
    assert D(then["revenue"]) == D("1200.00") and D(then["margin_percent"]) == D("50.0")
    assert D(changes["revenue"]) == D("335.0") and D(changes["orders"]) == D("200.0")
    assert D(changes["average_order_value"]) == D("45.0") and D(changes["gross_profit"]) == D("290.0")
    assert changes["recovered_revenue"] is None  # nothing recovered before
    assert D(changes["margin_points"]) == D("-5.2")


@pytest.mark.parametrize(
    "extra, revenue, orders",
    [
        pytest.param({"segment": "DEALER"}, "1620.00", 1, id="customer group"),
        pytest.param({"payment_method": "BANK_TRANSFER"}, "2820.00", 2, id="payment method"),
        # The parent category reaches A through its child; B is elsewhere.
        pytest.param("category", "3480.00", 2, id="category with its children"),
        pytest.param("brand", "3480.00", 2, id="brand"),
    ],
)
async def test_the_filters_narrow_every_figure(client, admin_user, shop, extra, revenue, orders):
    if extra == "category":
        extra = {"category_id": shop.parent.id}
    elif extra == "brand":
        extra = {"brand_id": shop.brand.id}
    now = (await _get(client, admin_user, "summary", shop.window(**extra)))["current"]
    assert D(now["revenue"]) == D(revenue) and now["orders"] == orders


async def test_an_unknown_cost_is_left_out_of_the_margin_not_counted_as_free(
    client, admin_user, shop, db_session: AsyncSession
):
    shop.b.variant.cost_price = None
    await db_session.commit()
    now = (await _get(client, admin_user, "summary", shop.window()))["current"]
    # Only A's lines carry a cost: 2900 before VAT against 1500.
    assert D(now["gross_profit"]) == D("1400.00") and D(now["margin_percent"]) == D("48.3")
    assert D(now["costed_share_percent"]) == D("66.7")
    assert now["estimated_cost_lines"] == 0


# ── Trend ────────────────────────────────────────────────────────────────────


async def test_the_trend_is_daily_with_every_day_present(client, admin_user, shop):
    body = await _get(client, admin_user, "trend", shop.window())
    assert body["granularity"] == "day" and len(body["points"]) == 31
    by_day = {p["start"][:10]: p for p in body["points"]}
    assert D(by_day[f"{shop.year}-10-10"]["revenue"]) == D("2400.00")
    assert D(by_day[f"{shop.year}-10-11"]["revenue"]) == D("1620.00")
    assert by_day[f"{shop.year}-10-01"]["orders"] == 0
    assert sum(D(p["revenue"]) for p in body["points"]) == D("5220.00")


async def test_one_day_goes_by_the_hour_and_a_long_period_by_the_week(client, admin_user, shop):
    day = await _get(client, admin_user, "trend", {"from": f"{shop.year}-10-10", "to": f"{shop.year}-10-10"})
    assert day["granularity"] == "hour" and len(day["points"]) == 24
    noon = next(p for p in day["points"] if D(p["revenue"]))
    assert noon["start"].startswith(f"{shop.year}-10-10T12:00:00") and D(noon["revenue"]) == D("2400.00")

    weeks = await _get(client, admin_user, "trend", {"from": f"{shop.year}-06-01", "to": f"{shop.year}-12-31"})
    assert weeks["granularity"] == "week"
    assert all(datetime.fromisoformat(p["start"]).weekday() == 0 for p in weeks["points"])
    assert sum(D(p["revenue"]) for p in weeks["points"]) == D("6420.00")  # September's sale too


# ── Customer groups and best sellers ─────────────────────────────────────────


async def test_each_customer_group_has_its_share_and_margin(client, admin_user, shop):
    rows = {r["segment"]: r for r in await _get(client, admin_user, "segments", shop.window())}
    assert list(rows) == ["RETAIL", "CAFE", "WHOLESALE", "DEALER"]
    assert D(rows["RETAIL"]["revenue"]) == D("3600.00") and D(rows["RETAIL"]["share_percent"]) == D("69.0")
    assert D(rows["DEALER"]["revenue"]) == D("1620.00") and D(rows["DEALER"]["share_percent"]) == D("31.0")
    assert D(rows["RETAIL"]["margin_percent"]) == D("46.7") and D(rows["DEALER"]["margin_percent"]) == D("40.7")
    assert rows["CAFE"]["orders"] == 0 and rows["CAFE"]["margin_percent"] is None


@pytest.mark.parametrize(
    "by, sort, first, second",
    [
        pytest.param("product", "revenue", ("A", "3480.00", 3), ("B", "1740.00", 3), id="products by revenue"),
        # Three of each: the revenue breaks the tie.
        pytest.param("product", "quantity", ("A", "3480.00", 3), ("B", "1740.00", 3), id="products by quantity"),
        pytest.param("category", "revenue", ("Gövde", "3480.00", 3), ("Kömür", "1740.00", 3), id="categories"),
        pytest.param("brand", "revenue", ("Marka X", "3480.00", 3), ("Marka Y", "1740.00", 3), id="brands"),
    ],
)
async def test_best_sellers(client, admin_user, shop, by, sort, first, second):
    rows = await _get(client, admin_user, "top", shop.window(by=by, sort=sort))
    got = [(r["name"], D(r["revenue"]), r["quantity"]) for r in rows]
    assert len(got) == 2
    for (name, revenue, qty), (want_name, want_revenue, want_qty) in zip(got, (first, second)):
        assert want_name in name and revenue == D(want_revenue) and qty == want_qty


# ── Card against havale ──────────────────────────────────────────────────────


async def test_card_and_havale_conversion_and_cancellation(client, admin_user, shop):
    rows = {r["payment_method"]: r for r in await _get(client, admin_user, "payment-methods", shop.window())}
    card, havale = rows["CREDIT_CARD"], rows["BANK_TRANSFER"]
    assert (card["placed"], card["paid"], card["cancelled"], card["failed"], card["pending"]) == (3, 1, 1, 1, 0)
    assert D(card["conversion_percent"]) == D("33.3") and D(card["cancellation_percent"]) == D("66.7")
    assert D(card["revenue"]) == D("2400.00")
    # The one still waiting counts towards neither rate.
    assert (havale["placed"], havale["paid"], havale["pending"]) == (3, 2, 1)
    assert D(havale["conversion_percent"]) == D("100.0") and D(havale["cancellation_percent"]) == D("0.0")
    assert D(havale["revenue"]) == D("2820.00")


# ── Profitability ────────────────────────────────────────────────────────────


async def test_profit_by_product_flags_margins_under_the_threshold(client, admin_user, shop):
    rows = await _get(client, admin_user, "profitability", shop.window(threshold="40"))
    a, b = rows
    assert shop.a.base_code == a["code"] and shop.b.base_code == b["code"]
    assert (D(a["revenue_excl_vat"]), D(a["cost"]), D(a["gross_profit"]), D(a["margin_percent"])) == (
        D("2900.00"), D("1500.00"), D("1400.00"), D("48.3")
    )
    assert (D(b["gross_profit"]), D(b["margin_percent"]), b["estimated"]) == (D("550.00"), D("37.9"), True)
    assert a["below_threshold"] is False and b["below_threshold"] is True

    by_margin = await _get(client, admin_user, "profitability", shop.window(sort="margin"))
    assert [r["code"] for r in by_margin] == [shop.b.base_code, shop.a.base_code]


async def test_list_price_margins_per_group_raise_the_erosion_alert(client, admin_user, shop):
    rows = await _get(client, admin_user, "tier-margins", {"brand_id": shop.brand.id})
    # Lowest margin first; C has a cost but no prices, so it comes last.
    assert [r["code"] for r in rows] == [shop.a.base_code, shop.e.base_code, shop.c.base_code]
    a, e, c = rows
    tiers = {t["segment"]: t for t in a["tiers"]}
    # Perakende: 1000 against 500, and 930 after the 7% a large transfer takes.
    retail = tiers["RETAIL"]
    assert (D(retail["margin_percent"]), D(retail["havale_rate_percent"]), D(retail["havale_price_excl_vat"])) == (
        D("50.0"), D("7.0"), D("930.00")
    )
    assert D(retail["havale_margin_percent"]) == D("46.2") and retail["below_threshold"] is False
    # Bayi at 550 against 500 is 9.1%; paid by havale, 511.50 leaves 2.2%.
    dealer = tiers["DEALER"]
    assert D(dealer["margin_percent"]) == D("9.1") and D(dealer["havale_margin_percent"]) == D("2.2")
    assert dealer["below_threshold"] is True and D(a["lowest_margin_percent"]) == D("2.2")
    assert tiers["CAFE"]["price_excl_vat"] is None and tiers["CAFE"]["havale_margin_percent"] is None
    assert a["alert"] is True
    # E's cost is USD 10 at today's 40: 400 against Bayi's 450, and E's Bayi
    # price never takes the havale discount — its switch is off.
    e_dealer = next(t for t in e["tiers"] if t["segment"] == "DEALER")
    assert e_dealer["havale_rate_percent"] is None and e_dealer["havale_margin_percent"] is None
    assert D(e["cost"]) == D("400.00") and D(e["lowest_margin_percent"]) == D("11.1") and e["alert"] is True
    assert c["alert"] is False and c["lowest_margin_percent"] is None

    alerts = await _get(client, admin_user, "tier-margins", {"brand_id": shop.brand.id, "only_alerts": "true"})
    assert [r["code"] for r in alerts] == [shop.a.base_code, shop.e.base_code]
    # At 5% only the transfer's erosion of A's Bayi price is left; at 2%, nothing.
    five = await _get(client, admin_user, "tier-margins", {"brand_id": shop.brand.id, "threshold": "5", "only_alerts": "true"})
    assert [r["code"] for r in five] == [shop.a.base_code]
    two = await _get(client, admin_user, "tier-margins", {"brand_id": shop.brand.id, "threshold": "2", "only_alerts": "true"})
    assert two == []


# ── Stock and missing costs ──────────────────────────────────────────────────


async def test_stock_not_sold_in_sixty_days(client, admin_user, shop):
    rows = await _get(client, admin_user, "slow-movers", {"brand_id": shop.brand.id, "days": "60"})
    # A sold five days ago; D is new; E has no stock.
    assert [r["code"] for r in rows] == [shop.c.base_code]
    assert rows[0]["stock"] == 5 and D(rows[0]["stock_value"]) == D("500.00") and rows[0]["last_sold_at"] is None


async def test_products_missing_their_cost(client, admin_user, shop):
    rows = await _get(client, admin_user, "missing-costs", {"brand_id": shop.brand.id})
    assert [r["code"] for r in rows] == [shop.d.base_code]
    assert rows[0]["stock"] == 3 and rows[0]["is_published"] is False and rows[0]["sold_quantity_90d"] == 0


# ── Excel'e Aktar ────────────────────────────────────────────────────────────


async def test_best_sellers_as_an_excel_file(client, admin_user, shop):
    resp = await client.get(f"{BASE}/export/top", headers=auth_header(admin_user), params=shop.window(segment="DEALER"))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    assert f"rapor-top-{shop.year}1001-{shop.year}1031.xlsx" in resp.headers["content-disposition"]

    ws = load_workbook(BytesIO(resp.content)).active
    assert ws["A1"].value == "En Çok Satan Ürünler"
    assert "Müşteri grubu: Bayi" in ws["A2"].value and f"01.10.{shop.year}" in ws["A2"].value
    assert [c.value for c in ws[4]] == ["Ürün Kodu", "Ürün", "Adet", "Ciro (KDV dahil)", "Sipariş"]
    rows = [[c.value for c in row] for row in ws.iter_rows(min_row=5)]
    assert rows == [[shop.a.base_code, shop.a.name, 1, D("1080.00"), 1], [shop.b.base_code, shop.b.name, 1, D("540.00"), 1]]
    assert ws["D5"].number_format.endswith('"₺"')


@pytest.mark.parametrize(
    "report", ["trend", "segments", "top", "slow-movers", "payment-methods", "profitability", "tier-margins", "missing-costs"]
)
async def test_every_table_exports(client, admin_user, shop, report):
    resp = await client.get(
        f"{BASE}/export/{report}", headers=auth_header(admin_user), params=shop.window(brand_id=shop.brand.id)
    )
    assert resp.status_code == 200, resp.text
    ws = load_workbook(BytesIO(resp.content)).active
    assert ws["A1"].value and ws[4][0].value and "Marka: Marka X" in ws["A2"].value


# ── Who and what ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["summary", "export/top"])
async def test_only_admins_see_the_reports(client, customer_user, path):
    resp = await client.get(f"{BASE}/{path}", headers=auth_header(customer_user))
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"from": "2026-10-31", "to": "2026-10-01"}, id="backwards"),
        pytest.param({"from": "2020-01-01", "to": "2026-01-01"}, id="over two years"),
        pytest.param({"payment_method": "QUOTE"}, id="not a sale method"),
    ],
)
async def test_impossible_periods_and_filters_are_refused(client, admin_user, params):
    resp = await client.get(f"{BASE}/summary", headers=auth_header(admin_user), params=params)
    assert resp.status_code == 422


async def test_the_default_period_is_this_month(client, admin_user):
    body = await _get(client, admin_user, "summary", {})
    today = datetime.now(TR).date()
    assert body["period"] == {"date_from": today.replace(day=1).isoformat(), "date_to": today.isoformat()}
