"""The PDF price catalogue's data: POST /products/price-list (a B2B account's)
and POST /admin/pricing/custom-catalog (an admin's, from the storefront).

Covers:
- Which groups each account may put in a catalogue: Bayi → Toptan/Kafe/Perakende,
  Toptan → Kafe/Perakende, Kafe → Kafe/Perakende; never its own buying price
  above that, and nothing at all for retail or anonymous callers
- Screen order kept, unpublished products left out, missing prices omitted
- Both VAT sides returned, in TRY
- A business group priced in USD/EUR comes as entered; retail always in TRY
- The admin's catalogue: any one group, Bayi included, for a section's products
  in screen order; refused to every account that is not an admin
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import products as products_api
from app.models.catalog import Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services.exchange_rates import Rates, RatesUnavailable
from tests.conftest import auth_header

URL = "/api/v1/products/price-list"
ADMIN_URL = "/api/v1/admin/pricing/custom-catalog"
PRICES = {  # incl. VAT at 20%
    SegmentType.DEALER: Decimal("1200.00"),
    SegmentType.WHOLESALE: Decimal("1320.00"),
    SegmentType.CAFE: Decimal("1440.00"),
    SegmentType.RETAIL: Decimal("1800.00"),
}


async def _account(db: AsyncSession, segments: dict[SegmentType, CustomerSegment], code: SegmentType) -> User:
    user = User(
        email=f"{code.value.lower()}-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x",
        role=UserRole.CUSTOMER, segment_id=segments[code].id, is_active=True, is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _product(db: AsyncSession, segments, *, published=True, prices=PRICES, currency="TRY") -> Product:
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"PL-{suffix}", name=f"Katalog {suffix}", tags=[], is_published=published,
                      image_url=f"https://cdn.example.com/{suffix}.jpg")
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"PL-{suffix}")
    db.add(variant)
    await db.flush()
    for code, incl in prices.items():
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency=currency,
            price_excl_tax=(incl / Decimal("1.2")).quantize(Decimal("0.01")), price_incl_tax=incl, vat_rate=Decimal("20"),
        ))
    await db.commit()
    return product


@pytest.mark.parametrize(
    ("own", "allowed", "refused"),
    [
        (SegmentType.DEALER, ["WHOLESALE", "CAFE", "RETAIL"], ["DEALER"]),
        (SegmentType.WHOLESALE, ["CAFE", "RETAIL"], ["WHOLESALE", "DEALER"]),
        (SegmentType.CAFE, ["CAFE", "RETAIL"], ["WHOLESALE", "DEALER"]),
    ],
)
async def test_each_account_gets_only_the_groups_below_it(
    client: AsyncClient, db_session: AsyncSession, segments, own, allowed, refused
):
    user = await _account(db_session, segments, own)
    product = await _product(db_session, segments)
    h = auth_header(user)
    ok = await client.post(URL, headers=h, json={"product_ids": [str(product.id)], "groups": allowed})
    assert ok.status_code == 200, ok.text
    assert sorted(ok.json()[0]["prices"]) == sorted(allowed)
    for group in refused:
        resp = await client.post(URL, headers=h, json={"product_ids": [str(product.id)], "groups": ["RETAIL", group]})
        assert resp.status_code == 403, (group, resp.text)


async def test_retail_and_anonymous_callers_get_nothing(
    client: AsyncClient, db_session: AsyncSession, segments, customer_user: User
):
    product = await _product(db_session, segments)
    body = {"product_ids": [str(product.id)], "groups": ["RETAIL"]}
    assert (await client.post(URL, headers=auth_header(customer_user), json=body)).status_code == 403
    assert (await client.post(URL, json=body)).status_code in (401, 403)


async def test_screen_order_published_only_and_both_vat_sides(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _account(db_session, segments, SegmentType.CAFE)
    first = await _product(db_session, segments)
    hidden = await _product(db_session, segments, published=False)
    no_cafe = await _product(db_session, segments, prices={SegmentType.RETAIL: Decimal("600.00")})
    last = await _product(db_session, segments)
    ids = [last.id, hidden.id, no_cafe.id, first.id, last.id]
    resp = await client.post(URL, headers=auth_header(user), json={"product_ids": [str(i) for i in ids], "groups": ["CAFE", "RETAIL"]})
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert [i["product_id"] for i in items] == [str(last.id), str(no_cafe.id), str(first.id)]
    assert sorted(items[1]["prices"]) == ["RETAIL"]  # not sold to kafe: no kafe price
    cafe = items[0]["prices"]["CAFE"]
    assert (cafe["currency"], Decimal(cafe["price_excl_tax"]), Decimal(cafe["price_incl_tax"])) == (
        "TRY", Decimal("1200.00"), Decimal("1440.00"),
    )
    assert items[0]["image_url"].startswith("https://cdn.example.com/")


@pytest.mark.parametrize("group", list(PRICES))
async def test_an_admin_gets_any_one_group(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User, group: SegmentType
):
    product = await _product(db_session, segments)
    resp = await client.post(
        ADMIN_URL, headers=auth_header(admin_user), json={"product_ids": [str(product.id)], "segment": group.value}
    )
    assert resp.status_code == 200, resp.text
    [item] = resp.json()
    assert list(item["prices"]) == [group.value]  # that group only, Bayi too
    price = item["prices"][group.value]
    assert (price["currency"], Decimal(price["price_incl_tax"])) == ("TRY", PRICES[group])
    assert (item["code"], item["name"]) == (product.base_code, product.name)


async def test_the_admin_catalogue_keeps_the_section_as_shown(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    first = await _product(db_session, segments)
    hidden = await _product(db_session, segments, published=False)
    retail_only = await _product(db_session, segments, prices={SegmentType.RETAIL: Decimal("600.00")})
    last = await _product(db_session, segments)
    ids = [last.id, hidden.id, retail_only.id, first.id, last.id, uuid.uuid4()]
    resp = await client.post(
        ADMIN_URL, headers=auth_header(admin_user), json={"product_ids": [str(i) for i in ids], "segment": "DEALER"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    # Screen order, each once; unpublished and unknown ones out.
    assert [i["product_id"] for i in items] == [str(last.id), str(retail_only.id), str(first.id)]
    assert items[1]["prices"] == {}  # not sold to bayi: the catalogue leaves it off


async def test_only_an_admin_gets_the_storefront_catalogue(
    client: AsyncClient, db_session: AsyncSession, segments, customer_user: User
):
    product = await _product(db_session, segments)
    body = {"product_ids": [str(product.id)], "segment": "DEALER"}
    for code in (SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE):
        user = await _account(db_session, segments, code)
        assert (await client.post(ADMIN_URL, headers=auth_header(user), json=body)).status_code == 403, code
    assert (await client.post(ADMIN_URL, headers=auth_header(customer_user), json=body)).status_code == 403
    assert (await client.post(ADMIN_URL, json=body)).status_code in (401, 403)


async def test_the_admin_catalogue_takes_one_known_group_and_a_bounded_list(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    product = await _product(db_session, segments)
    h = auth_header(admin_user)
    for body in (
        {"product_ids": [str(product.id)], "segment": "VIP"},
        {"product_ids": [str(product.id)]},
        {"product_ids": [], "segment": "RETAIL"},
        {"product_ids": [str(uuid.uuid4()) for _ in range(201)], "segment": "RETAIL"},
    ):
        assert (await client.post(ADMIN_URL, headers=h, json=body)).status_code == 422, body


USD_PRICES = {  # incl. VAT at 20%, in USD
    SegmentType.DEALER: Decimal("24.00"),
    SegmentType.WHOLESALE: Decimal("26.40"),
    SegmentType.CAFE: Decimal("28.80"),
    SegmentType.RETAIL: Decimal("36.00"),
}


def _rates(usd: str | None):
    async def get_rates():
        if usd is None:
            raise RatesUnavailable("TCMB down")
        from datetime import date

        return Rates(bulletin_date=date(2026, 10, 30), try_per_unit={"USD": Decimal(usd), "EUR": Decimal("55.00")})

    return get_rates


async def test_a_business_group_priced_in_dollars_comes_in_dollars_retail_in_lira(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User, monkeypatch
):
    monkeypatch.setattr(products_api, "get_rates", _rates("50.00"))
    product = await _product(db_session, segments, prices=USD_PRICES, currency="USD")
    prices = {}
    for group in USD_PRICES:
        resp = await client.post(
            ADMIN_URL, headers=auth_header(admin_user), json={"product_ids": [str(product.id)], "segment": group.value}
        )
        assert resp.status_code == 200, resp.text
        prices.update(resp.json()[0]["prices"])
    for group in (SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE):
        assert (prices[group.value]["currency"], Decimal(prices[group.value]["price_incl_tax"])) == ("USD", USD_PRICES[group])
    retail = prices["RETAIL"]
    assert (retail["currency"], Decimal(retail["price_incl_tax"])) == ("TRY", Decimal("1800.00"))


async def test_with_tcmb_down_business_dollars_still_come_and_retail_waits(
    client: AsyncClient, db_session: AsyncSession, segments, monkeypatch
):
    monkeypatch.setattr(products_api, "get_rates", _rates(None))
    user = await _account(db_session, segments, SegmentType.CAFE)
    product = await _product(db_session, segments, prices=USD_PRICES, currency="USD")
    resp = await client.post(URL, headers=auth_header(user), json={"product_ids": [str(product.id)], "groups": ["CAFE", "RETAIL"]})
    assert resp.status_code == 200, resp.text
    [item] = resp.json()
    assert list(item["prices"]) == ["CAFE"]
    assert item["prices"]["CAFE"]["currency"] == "USD"
