"""The homepage's Sultan Puan row (GET /products/sultanpuan): the products whose
current retail price falls in the live earning band, which follows the band
and the switch saved in "Parametreler"."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services.store_parameters import DEFAULT_PARAMETERS, save_parameters
from tests.conftest import auth_header
from tests.test_sultan_puan import _set_loyalty

URL = "/api/v1/products/sultanpuan"


@pytest.fixture(autouse=True)
async def restore_defaults(db_session: AsyncSession):
    """The test database is shared by the whole session: put the shop's rules back."""
    yield
    await db_session.rollback()
    await save_parameters(db_session, DEFAULT_PARAMETERS)
    await db_session.commit()


@pytest.fixture
async def priced(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]):
    """One product per retail price (TRY, KDV dahil), in stock; by price."""
    suffix = uuid.uuid4().hex[:6]
    retail = segments[SegmentType.RETAIL].id
    dealer = segments[SegmentType.DEALER].id
    made: dict[str, Product] = {}
    variants: dict[str, ProductVariant] = {}
    for price in ("19999.99", "20000.00", "24000.00", "29999.99", "30000.00"):
        product = Product(base_code=f"SP-{price}-{suffix}", name=f"Puanlık {price} {suffix}", tags=[])
        db_session.add(product)
        await db_session.flush()
        variant = ProductVariant(product_id=product.id, sku=f"SP-{price}-{suffix}")
        db_session.add(variant)
        await db_session.flush()
        incl = Decimal(price)
        db_session.add_all([
            VariantPrice(variant_id=variant.id, segment_id=retail, currency="TRY",
                         price_excl_tax=(incl / Decimal("1.2")).quantize(Decimal("0.01")), price_incl_tax=incl,
                         vat_rate=Decimal("20.00")),
            # A dealer price far inside the band: it must not matter.
            VariantPrice(variant_id=variant.id, segment_id=dealer, currency="TRY",
                         price_excl_tax=Decimal("20833.33"), price_incl_tax=Decimal("25000.00"),
                         vat_rate=Decimal("20.00")),
            Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=5),
        ])
        made[price], variants[price] = product, variant
    await db_session.commit()
    yield made, variants
    ids = [p.id for p in made.values()]
    vids = [v.id for v in variants.values()]
    await db_session.execute(delete(FlashSale).where(FlashSale.variant_id.in_(vids)))
    await db_session.execute(delete(VariantPrice).where(VariantPrice.variant_id.in_(vids)))
    await db_session.execute(delete(Inventory).where(Inventory.variant_id.in_(vids)))
    await db_session.execute(delete(ProductVariant).where(ProductVariant.id.in_(vids)))
    await db_session.execute(delete(Product).where(Product.id.in_(ids)))
    await db_session.commit()


async def _row(client: AsyncClient, headers: dict | None = None) -> dict:
    resp = await client.get(URL, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _mine(row: dict, made: dict[str, Product]) -> list[str]:
    """Our products in the row, as their prices, in the row's order."""
    by_id = {str(p.id): price for price, p in made.items()}
    return [by_id[p["id"]] for p in row["products"] if p["id"] in by_id]


async def test_the_default_band_lists_its_products_cheapest_first(client: AsyncClient, priced):
    made, _ = priced
    row = await _row(client)
    assert row["enabled"] is True
    assert Decimal(row["rate_percent"]) == Decimal("10")
    assert (Decimal(row["band_min"]), Decimal(row["band_max"])) == (Decimal("20000"), Decimal("29999.99"))
    # Both bounds count, as they do when the points are earned.
    assert _mine(row, made) == ["20000.00", "24000.00", "29999.99"]


async def test_a_new_band_is_a_new_list(client: AsyncClient, admin_user: User, priced):
    made, _ = priced
    await _set_loyalty(client, admin_user, band_min=Decimal("25000"), band_max=Decimal("40000"))
    row = await _row(client)
    assert (Decimal(row["band_min"]), Decimal(row["band_max"])) == (Decimal("25000"), Decimal("40000"))
    assert _mine(row, made) == ["29999.99", "30000.00"]


async def test_with_the_programme_off_the_row_is_empty(client: AsyncClient, admin_user: User, priced):
    await _set_loyalty(client, admin_user, enabled=False)
    row = await _row(client)
    assert row["enabled"] is False and row["products"] == []


async def test_unpublished_products_are_left_out(client: AsyncClient, db_session: AsyncSession, priced):
    made, _ = priced
    made["24000.00"].is_published = False
    await db_session.commit()
    assert _mine(await _row(client), made) == ["20000.00", "29999.99"]


async def test_a_live_deal_price_is_the_price_that_counts(client: AsyncClient, db_session: AsyncSession, priced):
    """30.000 TL on sale at 27.000 TL is in the band; 20.000 TL on sale at
    18.000 TL has left it."""
    made, variants = priced
    now = datetime.now(timezone.utc)
    for price, sale in (("30000.00", "27000.00"), ("20000.00", "18000.00")):
        made[price].is_opportunity = True  # a deal is live only while its product's "Fırsat" switch is on
        db_session.add(FlashSale(variant_id=variants[price].id, sale_price_incl_tax=Decimal(sale),
                                 starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=1)))
    await db_session.commit()
    # Ordered by the price that counts: 24.000, 27.000 (the sale), 29.999,99.
    assert _mine(await _row(client), made) == ["24000.00", "30000.00", "29999.99"]


async def test_a_dealer_gets_the_retail_row(
    client: AsyncClient, db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment], priced
):
    """Priced as retail whoever asks: the dealer's own prices (all 25.000 TL
    here) would put every product in the band."""
    made, _ = priced
    dealer = User(email=f"dealer-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x", role=UserRole.CUSTOMER,
                  segment_id=segments[SegmentType.DEALER].id, is_active=True, is_verified=True)
    db_session.add(dealer)
    await db_session.commit()
    row = await _row(client, auth_header(dealer))
    assert _mine(row, made) == ["20000.00", "24000.00", "29999.99"]
    assert {p["variants"][0]["price"]["price_incl_tax"] for p in row["products"] if p["id"] == str(made["24000.00"].id)} == {"24000.00"}
