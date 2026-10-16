"""The B2B PDF price catalogue's data: POST /products/price-list.

Covers:
- Which groups each account may put in a catalogue: Bayi → Toptan/Kafe/Perakende,
  Toptan → Kafe/Perakende, Kafe → Kafe/Perakende; never its own buying price
  above that, and nothing at all for retail or anonymous callers
- Screen order kept, unpublished products left out, missing prices omitted
- Both VAT sides returned, in TRY
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from tests.conftest import auth_header

URL = "/api/v1/products/price-list"
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


async def _product(db: AsyncSession, segments, *, published=True, prices=PRICES) -> Product:
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
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
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
