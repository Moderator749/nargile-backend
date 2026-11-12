"""Tests for the public categories endpoint and catalog category/search filters."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product


@pytest.fixture
async def filter_fixture(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Filtre {suffix}", slug=f"filtre-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    inside = Product(
        base_code=f"FLT-{suffix}", name=f"ÖZEL NARGİLE {suffix}", category_id=cat.id, tags=[],
        image_url=f"https://www.nargilemalzemesi.com/image/cache/catalog/{suffix}-800x800.jpg",
    )
    outside = Product(base_code=f"OUT-{suffix}", name=f"Diğer Ürün {suffix}", tags=[])
    literal = Product(base_code=f"PCT-{suffix}", name=f"Yüzde 100% {suffix}", tags=[])
    db_session.add_all([inside, outside, literal])
    await db_session.commit()
    yield cat, inside, outside, literal, suffix

    # Clean up: these rows would otherwise appear as real catalog tabs/products.
    await db_session.execute(delete(Product).where(Product.id.in_([inside.id, outside.id, literal.id])))
    await db_session.execute(delete(Category).where(Category.id == cat.id))
    await db_session.commit()


async def _ids(client: AsyncClient, **params) -> set[str]:
    resp = await client.get("/api/v1/products/", params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return {p["id"] for p in resp.json()}


async def test_list_categories_is_public(client: AsyncClient, filter_fixture):
    cat, *_ = filter_fixture
    resp = await client.get("/api/v1/categories/")
    assert resp.status_code == 200
    entry = next(c for c in resp.json() if c["id"] == cat.id)
    assert {k: entry[k] for k in ("name", "slug", "parent_id", "sort_order", "image_url")} == {
        "name": cat.name, "slug": cat.slug, "parent_id": None, "sort_order": 0, "image_url": None,
    }
    # The one product filed under it, dated by its own last edit: the sitemap's
    # <lastmod> for the category page.
    assert entry["product_count"] == 1
    assert entry["updated_at"] is not None


async def test_filter_by_category_slug(client: AsyncClient, filter_fixture):
    cat, inside, outside, *_ = filter_fixture
    ids = await _ids(client, category=cat.slug)
    assert ids == {str(inside.id)}


async def test_unknown_category_returns_empty(client: AsyncClient):
    assert await _ids(client, category=f"yok-{uuid.uuid4().hex}") == set()


@pytest.mark.parametrize("term", ["özel nargile", "ÖZEL NARGİLE", "Özel Nargıle"])
async def test_search_folds_turkish_case(client: AsyncClient, filter_fixture, term: str):
    _, inside, _, _, suffix = filter_fixture
    assert str(inside.id) in await _ids(client, q=f"{term} {suffix}")


async def test_search_treats_wildcards_literally(client: AsyncClient, filter_fixture):
    _, inside, outside, literal, suffix = filter_fixture
    ids = await _ids(client, q=f"100% {suffix}")
    assert ids == {str(literal.id)}
    # A bare "%" must not match everything.
    assert str(outside.id) not in await _ids(client, q=f"%{suffix[:0]}_")


async def test_search_and_category_combine(client: AsyncClient, filter_fixture):
    cat, inside, *_ , suffix = filter_fixture
    assert await _ids(client, category=cat.slug, q=suffix) == {str(inside.id)}
    assert await _ids(client, category=cat.slug, q="bulunmayan-terim") == set()


async def test_catalog_exposes_product_image_url(client: AsyncClient, filter_fixture):
    _cat, inside, outside, _literal, suffix = filter_fixture
    resp = await client.get("/api/v1/products/", params={"q": suffix, "limit": 100})
    images = {p["id"]: p["image_url"] for p in resp.json()}
    assert images[str(inside.id)] == inside.image_url
    assert images[str(outside.id)] is None


async def test_catalog_exposes_when_a_product_last_changed(
    client: AsyncClient, db_session: AsyncSession, filter_fixture
):
    """`updated_at` is the sitemap's <lastmod>: present on every product, and
    moved forward by an edit to the product row."""
    from datetime import datetime

    _cat, inside, _outside, _literal, suffix = filter_fixture

    async def stamp() -> datetime:
        resp = await client.get("/api/v1/products/", params={"q": suffix, "limit": 100})
        found = {p["id"]: p["updated_at"] for p in resp.json()}
        assert found[str(inside.id)], "every product carries updated_at"
        return datetime.fromisoformat(found[str(inside.id)])

    before = await stamp()
    inside.description = "Yeni açıklama"
    await db_session.commit()
    assert await stamp() > before


@pytest.mark.parametrize("term", ["ozel nargile", "OZEL NARGILE", "özel nargıle"])
async def test_search_folds_turkish_letters(client: AsyncClient, filter_fixture, term: str):
    """A phone's keyboard types "lule" for "lüle": the letters fold to ASCII on both sides."""
    _, inside, _, _, suffix = filter_fixture
    assert str(inside.id) in await _ids(client, q=f"{term} {suffix}")


async def test_search_finds_a_category_by_its_name(client: AsyncClient, db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Lüleler {suffix}", slug=f"luleler-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    filed = Product(base_code=f"KLR-{suffix}", name=f"Killer {suffix}", category_id=cat.id, tags=[])
    db_session.add(filed)
    await db_session.commit()
    try:
        assert str(filed.id) in await _ids(client, q=f"luleler {suffix}")
    finally:
        await db_session.execute(delete(Product).where(Product.id == filed.id))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()


async def test_sold_out_products_come_last(client: AsyncClient, db_session: AsyncSession):
    """Whatever the order, a product with no colour in the caller's stock sinks to the end."""
    from app.models.catalog import Inventory, ProductVariant, WarehouseType

    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Stok {suffix}", slug=f"stok-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    products = {
        name: Product(base_code=f"{name}-{suffix}", name=f"{name} {suffix}", category_id=cat.id, tags=[])
        for name in ("Aaa", "Bbb", "Ccc")
    }
    db_session.add_all(products.values())
    await db_session.flush()
    for name, quantity in (("Aaa", None), ("Bbb", 5), ("Ccc", 0)):
        variant = ProductVariant(product_id=products[name].id, sku=f"{name}-{suffix}")
        db_session.add(variant)
        await db_session.flush()
        if quantity is not None:
            db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=quantity))
    await db_session.commit()
    try:
        resp = await client.get("/api/v1/products/", params={"category": cat.slug})
        assert [p["name"] for p in resp.json()] == [f"Bbb {suffix}", f"Aaa {suffix}", f"Ccc {suffix}"]
    finally:
        ids = [p.id for p in products.values()]
        await db_session.execute(delete(Inventory).where(Inventory.variant_id.in_(
            ProductVariant.__table__.select().with_only_columns(ProductVariant.id).where(ProductVariant.product_id.in_(ids))
        )))
        await db_session.execute(delete(ProductVariant).where(ProductVariant.product_id.in_(ids)))
        await db_session.execute(delete(Product).where(Product.id.in_(ids)))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()


async def test_cart_lines_link_a_visitors_lines_without_a_count(client: AsyncClient, catalog):
    """A guest's cart learns each line's page, code and whether any is left — never how many."""
    normal_v, heavy_v = catalog
    resp = await client.get("/api/v1/products/cart-lines", params={"ids": [str(normal_v.id), str(uuid.uuid4())]})
    assert resp.status_code == 200, resp.text
    [line] = resp.json()
    assert line == {
        "variant_id": str(normal_v.id),
        "product_id": line["product_id"],
        "product_slug": line["product_slug"],
        "sku": normal_v.sku,
        "in_stock": True,
    }
    assert line["product_slug"]


async def test_stock_check_holds_a_quantity_to_the_stock_without_the_count(client: AsyncClient, catalog):
    normal_v, heavy_v = catalog
    gone = str(uuid.uuid4())
    resp = await client.post("/api/v1/products/stock-check", json={"items": [
        {"variant_id": str(normal_v.id), "quantity": 3},
        {"variant_id": str(heavy_v.id), "quantity": 9999},
        {"variant_id": gone, "quantity": 2},
    ]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"variant_id": str(normal_v.id), "quantity": 3, "capped": False},
        {"variant_id": str(heavy_v.id), "quantity": 500, "capped": True},
        {"variant_id": gone, "quantity": 0, "capped": True},
    ]


async def test_suggestions_fold_turkish_letters(client: AsyncClient, db_session: AsyncSession):
    """From the second letter: products as the search lists them, the matching categories, the total."""
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Lüleler {suffix}", slug=f"luleler-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    killer = Product(base_code=f"KL-{suffix}", name=f"Sırlı Lüle {suffix}", category_id=cat.id, tags=[])
    db_session.add(killer)
    await db_session.commit()
    try:
        resp = await client.get("/api/v1/products/suggest", params={"q": f"lule {suffix}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert [p["id"] for p in body["products"]] == [str(killer.id)]
        resp = await client.get("/api/v1/products/suggest", params={"q": f"luleler {suffix}"})
        assert resp.json()["categories"] == [{"name": cat.name, "slug": cat.slug}]
        assert (await client.get("/api/v1/products/suggest", params={"q": "l"})).status_code == 422
    finally:
        await db_session.execute(delete(Product).where(Product.id == killer.id))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()


async def test_summary_counts_a_listing_and_prices_it_as_its_cards(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The whole listing's size and price range, each product at the colour its card shows."""
    from decimal import Decimal

    from app.models.catalog import Inventory, ProductVariant, VariantPrice, WarehouseType
    from app.models.user import SegmentType

    retail_id = segments[SegmentType.RETAIL].id
    suffix = uuid.uuid4().hex[:6]
    cat = Category(name=f"Özet {suffix}", slug=f"ozet-{suffix}")
    db_session.add(cat)
    await db_session.flush()
    # (name, [(price, stock), …] in colour order)
    rows = {"Aaa": [(100, 3)], "Bbb": [(50, 0)], "Ccc": [(10, 0), (300, 2)]}
    products = []
    for name, colours in rows.items():
        product = Product(base_code=f"{name}-{suffix}", name=f"{name} {suffix}", category_id=cat.id, tags=[])
        db_session.add(product)
        await db_session.flush()
        products.append(product)
        for i, (price, stock) in enumerate(colours):
            variant = ProductVariant(product_id=product.id, sku=f"{name}-{i}-{suffix}", position=i)
            db_session.add(variant)
            await db_session.flush()
            db_session.add(VariantPrice(
                variant_id=variant.id, segment_id=retail_id, currency="TRY",
                price_excl_tax=Decimal(price), price_incl_tax=Decimal(price), vat_rate=Decimal("0"),
            ))
            db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=stock))
    await db_session.commit()
    try:
        resp = await client.get("/api/v1/products/summary", params={"category": cat.slug})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Ccc's card shows its second colour, the one in stock: 300, not 10.
        assert (body["total"], Decimal(body["price_min"]), Decimal(body["price_max"])) == (3, Decimal(50), Decimal(300))
        assert (await client.get("/api/v1/products/summary", params={"q": f"yok-{suffix}"})).json() == {
            "total": 0, "price_min": None, "price_max": None,
        }
    finally:
        ids = [p.id for p in products]
        variants = ProductVariant.__table__.select().with_only_columns(ProductVariant.id).where(ProductVariant.product_id.in_(ids))
        await db_session.execute(delete(VariantPrice).where(VariantPrice.variant_id.in_(variants)))
        await db_session.execute(delete(Inventory).where(Inventory.variant_id.in_(variants)))
        await db_session.execute(delete(ProductVariant).where(ProductVariant.product_id.in_(ids)))
        await db_session.execute(delete(Product).where(Product.id.in_(ids)))
        await db_session.execute(delete(Category).where(Category.id == cat.id))
        await db_session.commit()


async def test_a_product_without_a_brand_takes_the_one_its_name_carries(client: AsyncClient, db_session: AsyncSession):
    from app.models.brand import Brand

    suffix = uuid.uuid4().hex[:6]
    brand = Brand(name=f"Zeta {suffix}", slug=f"zeta-{suffix}", search_term=f"zeta{suffix}")
    db_session.add(brand)
    bare = Product(base_code=f"ZT-{suffix}", name=f"ZETA{suffix} Bowl - Nargile Lülesi", tags=[])
    own = Product(base_code=f"OW-{suffix}", name=f"Zeta{suffix} Bowl", brand="Kendi", tags=[])
    # The term inside another word is not the brand.
    inside = Product(base_code=f"IN-{suffix}", name=f"Xzeta{suffix} Bowl", tags=[])
    db_session.add_all([bare, own, inside])
    await db_session.commit()
    try:
        resp = await client.get("/api/v1/products/", params={"q": f"zeta{suffix}", "limit": 10})
        brands = {p["base_code"]: p["brand"] for p in resp.json()}
        assert brands == {bare.base_code: brand.name, own.base_code: "Kendi", inside.base_code: None}
    finally:
        await db_session.execute(delete(Product).where(Product.id.in_([bare.id, own.id, inside.id])))
        await db_session.execute(delete(Brand).where(Brand.id == brand.id))
        await db_session.commit()


@pytest.mark.parametrize(
    ("query", "means_bottles"),
    [
        ("yedek cam", True), ("nargile camı", True), ("Nargile Camı", True), ("cam", True), ("şişe camları", True),
        ("cam lüle", False), ("samsaris yedek cam", False), ("lüle haznesi cam", False),
        ("camel", False), ("kamp", False), ("çakmak", False),
    ],
)
def test_glass_means_the_bottles_unless_it_is_a_bowls(query, means_bottles):
    from app.api.v1.products import _fold_tr
    from app.services.search_synonyms import synonym_categories

    assert (synonym_categories(_fold_tr(query)) == ["siseler"]) is means_bottles


async def test_yedek_cam_finds_the_bottles_and_still_the_glass_parts(client: AsyncClient, db_session: AsyncSession):
    """"yedek cam" / "nargile camı" list the Nargile Şişeleri (named "Şişe"), and
    whatever is named with the words too; a bowl's glass is not a bottle."""
    from sqlalchemy import select

    suffix = uuid.uuid4().hex[:6]
    bottles = await db_session.scalar(select(Category).where(Category.slug == "siseler"))
    made = bottles is None
    if made:
        bottles = Category(name="Nargile Şişeleri", slug="siseler")
        db_session.add(bottles)
        await db_session.flush()
    bottle = Product(base_code=f"SS-{suffix}", name=f"Geçme Nargile Şişesi {suffix}", category_id=bottles.id, tags=[])
    insert = Product(base_code=f"LL-{suffix}", name=f"Samsaris Yedek Cam Lüle {suffix}", tags=[])
    db_session.add_all([bottle, insert])
    await db_session.commit()
    try:
        found = await _ids(client, q="yedek cam")
        assert {str(bottle.id), str(insert.id)} <= found
        assert str(bottle.id) in await _ids(client, q="nargile camı")
        # About a bowl's glass: the bowl part, not the bottles.
        bowl = await _ids(client, q="samsaris yedek cam")
        assert str(insert.id) in bowl and str(bottle.id) not in bowl
        # Inside another category the synonym stays a filter, not a way out of it.
        assert str(bottle.id) not in await _ids(client, q="yedek cam", category=f"yok-{suffix}")
        # Searching inside the bottles themselves: both category trees in one statement.
        assert str(bottle.id) in await _ids(client, q="yedek cam", category="siseler")

        resp = await client.get("/api/v1/products/suggest", params={"q": "yedek cam"})
        assert resp.status_code == 200, resp.text
        assert {"name": bottles.name, "slug": "siseler"} in resp.json()["categories"]
    finally:
        await db_session.execute(delete(Product).where(Product.id.in_([bottle.id, insert.id])))
        if made:
            await db_session.execute(delete(Category).where(Category.id == bottles.id))
        await db_session.commit()


@pytest.mark.parametrize(
    ("query", "means"),
    [
        ("nargile hortumu", ["marpuclar"]), ("silikon hortum", ["marpuclar"]), ("hortum yayı", []),
        ("ağızlık", ["ozel-sipsiler", "plastik-ve-slikon-sipsiler"]), ("nargile ağızlığı", ["ozel-sipsiler", "plastik-ve-slikon-sipsiler"]),
        ("hmd", ["kozlukler"]), ("kaloud lotus", ["kozlukler"]), ("copper balance hmd", ["kozlukler"]),
        ("şişe lastiği", ["contalar"]), ("lüle lastiği", ["contalar"]), ("plastik", []), ("plastik sipsi", []),
        ("nargile kafası", ["luleler"]), ("başlık", ["luleler"]), ("marpuç başlığı", []),
        ("köz makinesi", ["elektrikli-ocaklar", "gazli-ocaklar"]), ("kömür yakıcı", ["elektrikli-ocaklar", "gazli-ocaklar"]),
        ("köz ocağı", ["elektrikli-ocaklar", "gazli-ocaklar"]),
        ("mr wood", []), ("darkside", []), ("lüle", []), ("kafatası", []),
    ],
)
def test_colloquial_words_mean_their_shelves(query, means):
    from app.api.v1.products import _fold_tr
    from app.services.search_synonyms import synonym_categories

    assert synonym_categories(_fold_tr(query)) == means


@pytest.mark.parametrize(
    ("name", "query"),
    [("Mr.Wood Prisma Nargile Takımı", "mr wood"), ("Mr Wood Prisma", "mr.wood"), ("Darkside D-Shot Lüle", "dark side"),
     ("Black Coco's 26 er Kömür", "black coco"), ("Black Coco's 26 er Kömür", "blackcoco"), ("One Nation 27 er Kömür", "onenation")],
)
async def test_brand_spellings_meet_whatever_the_spaces_and_dots(client: AsyncClient, db_session: AsyncSession, name, query):
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"SP-{suffix}", name=f"{name} {suffix}", tags=[])
    db_session.add(product)
    await db_session.commit()
    try:
        assert str(product.id) in await _ids(client, q=query)
    finally:
        await db_session.execute(delete(Product).where(Product.id == product.id))
        await db_session.commit()


async def test_a_short_query_is_not_squashed_across_words(client: AsyncClient, db_session: AsyncSession):
    """"a.b" squashed is "ab": too short to stand for anything, so "Kara Bulut" is not found by it
    (as typed, "a.b" is in no name either)."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"KB-{suffix}", name=f"Kara Bulut {suffix}", tags=[])
    db_session.add(product)
    await db_session.commit()
    try:
        assert str(product.id) not in await _ids(client, q="a.b")
    finally:
        await db_session.execute(delete(Product).where(Product.id == product.id))
        await db_session.commit()


async def test_masa_folded_finds_both_but_masa_with_s_cedilla_means_the_tongs(client: AsyncClient, db_session: AsyncSession):
    """A phone without Turkish keys types "masa" for the tongs: folded, it finds
    them and the tables. "maşa", typed with ş, means the tongs only."""
    suffix = uuid.uuid4().hex[:6]
    tongs = Product(base_code=f"MS-{suffix}", name=f"Köz Maşası {suffix}", tags=[])
    table = Product(base_code=f"MT-{suffix}", name=f"Masaüstü Nargile Takımı {suffix}", tags=[])
    db_session.add_all([tongs, table])
    await db_session.commit()
    try:
        folded = await _ids(client, q="masa")
        assert {str(tongs.id), str(table.id)} <= folded
        typed = await _ids(client, q="maşa")
        assert str(tongs.id) in typed and str(table.id) not in typed
        # Capitals, as typed: still the tongs.
        assert str(tongs.id) in await _ids(client, q="MAŞA")
        # The suggestions put the name that holds it as typed first.
        resp = await client.get("/api/v1/products/suggest", params={"q": "masa"})
        order = [p["id"] for p in resp.json()["products"]]
        assert order.index(str(table.id)) < order.index(str(tongs.id))
    finally:
        await db_session.execute(delete(Product).where(Product.id.in_([tongs.id, table.id])))
        await db_session.commit()


async def test_a_turkish_word_still_reaches_a_shelf_its_name_or_synonym_means(client: AsyncClient, db_session: AsyncSession):
    """"ağızlık" is in no product's name: its synonym shelf still answers it."""
    from sqlalchemy import select

    suffix = uuid.uuid4().hex[:6]
    shelf = await db_session.scalar(select(Category).where(Category.slug == "ozel-sipsiler"))
    made = shelf is None
    if made:
        shelf = Category(name="Özel Sipsiler", slug="ozel-sipsiler")
        db_session.add(shelf)
        await db_session.flush()
    tip = Product(base_code=f"SP-{suffix}", name=f"Askılı Kehribar Sipsi {suffix}", category_id=shelf.id, tags=[])
    db_session.add(tip)
    await db_session.commit()
    try:
        assert str(tip.id) in await _ids(client, q="ağızlık")
    finally:
        await db_session.execute(delete(Product).where(Product.id == tip.id))
        if made:
            await db_session.execute(delete(Category).where(Category.id == shelf.id))
        await db_session.commit()


async def test_a_meta_description_copied_onto_many_products_is_flagged(client: AsyncClient, db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    boiler = f"Lüle çeşitleri peşin fiyatına 3 taksit {suffix}"
    copies = [Product(base_code=f"BP{i}-{suffix}", slug=f"boiler-{i}-{suffix}", name=f"Lüle {i} {suffix}", meta_description=boiler, tags=[]) for i in range(3)]
    own = Product(base_code=f"OWN-{suffix}", slug=f"own-{suffix}", name=f"Özel {suffix}", meta_description=f"Kendi açıklaması {suffix}", tags=[])
    db_session.add_all([*copies, own])
    await db_session.commit()
    try:
        assert (await client.get(f"/api/v1/products/boiler-0-{suffix}")).json()["meta_description_shared"] is True
        assert (await client.get(f"/api/v1/products/own-{suffix}")).json()["meta_description_shared"] is False
    finally:
        await db_session.execute(delete(Product).where(Product.base_code.like(f"%-{suffix}")))
        await db_session.commit()
