"""Scrape live product catalog from nargilemalzemesi.com and seed the database.

Fetches real products across key categories, creates the 4-tier pricing
matrix (RETAIL / CAFE / WHOLESALE / DEALER), and seeds dual-warehouse
inventory (RETAIL_STOCK + B2B_STOCK).

Idempotent: skips products whose base_code (site product code) already exists.
"""

import asyncio
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.core.database import async_session
from app.models.catalog import (
    Category,
    CurrencyType,
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.user import CustomerSegment, SegmentType

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = "https://www.nargilemalzemesi.com"

CATEGORIES_TO_SCRAPE = [
    ("Nargile Takımları", "nargile-takimlari", [
        "/paslanmaz-celik-nargile-takimlari",
        "/sultan-nargile-takimlari-ve-malzemeleri",
    ]),
    ("Kömürler", "komurler", [
        "/nargile-komuru",
    ]),
    ("Lüleler", "luleler", [
        "/nargile-lule-cesitleri",
    ]),
    ("Aksesuarlar", "aksesuarlar", [
        "/nargile-malzemeleri-ve-aksesuarlari",
    ]),
]

MAX_PRODUCTS_PER_URL = 24  # first page only

# B2B tier multipliers applied to scraped retail price (excl tax)
TIER_MULTIPLIERS = {
    SegmentType.RETAIL: Decimal("1.00"),
    SegmentType.CAFE: Decimal("0.85"),
    SegmentType.WHOLESALE: Decimal("0.70"),
    SegmentType.DEALER: Decimal("0.60"),
}

VAT_RATE = Decimal("18.00")
VAT_MULT = Decimal("1.18")

# Keywords that trigger HEAVY_CARGO tag
HEAVY_KEYWORDS = re.compile(r"kömür|komur|kömü|charcoal|kg\b|kilo", re.IGNORECASE)

RETAIL_STOCK_QTY = 50
B2B_STOCK_QTY = 250


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ScrapedProduct:
    name: str
    base_code: str
    price_text: str
    url: str
    category_slug: str
    price_excl_tax: Decimal = Decimal("0")
    tags: list[str] = field(default_factory=list)


# ── Parsing helpers ──────────────────────────────────────────────────────────

def parse_price(text: str) -> Decimal:
    """Parse '8.990,00TL' → Decimal('8990.00')."""
    cleaned = text.replace("TL", "").replace(".", "").replace(",", ".").strip()
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def incl_tax(excl: Decimal) -> Decimal:
    return (excl * VAT_MULT).quantize(Decimal("0.01"))


def make_sku(base_code: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base_code).strip("-").upper()
    return slug[:45] if slug else f"SKU-{id(base_code)}"


def detect_tags(name: str) -> list[str]:
    if HEAVY_KEYWORDS.search(name):
        return ["HEAVY_CARGO"]
    return []


# ── Scraper ──────────────────────────────────────────────────────────────────

async def scrape_category_page(
    client: httpx.AsyncClient, url: str, category_slug: str
) -> list[ScrapedProduct]:
    """Scrape one category page and return parsed products."""
    resp = await client.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products: list[ScrapedProduct] = []
    for card in soup.select(".product-layout"):
        name_el = card.select_one(".name a")
        code_el = card.select_one(".stat-2 span:last-child")
        price_el = card.select_one(".price-new") or card.select_one(".price-normal")

        if not name_el or not price_el:
            continue

        name = name_el.get_text(strip=True)
        code = code_el.get_text(strip=True) if code_el else ""
        price_text = price_el.get_text(strip=True)
        href = name_el.get("href", "")

        if not code or not name:
            continue

        price_incl = parse_price(price_text)
        if price_incl <= 0:
            continue

        # Reverse-engineer excl-tax price from the displayed incl-tax price
        price_excl = (price_incl / VAT_MULT).quantize(Decimal("0.01"))

        products.append(ScrapedProduct(
            name=name,
            base_code=code,
            price_text=price_text,
            url=href,
            category_slug=category_slug,
            price_excl_tax=price_excl,
            tags=detect_tags(name),
        ))

    return products


async def scrape_all() -> list[ScrapedProduct]:
    """Scrape all configured category URLs."""
    all_products: list[ScrapedProduct] = []
    seen_codes: set[str] = set()

    async with httpx.AsyncClient(
        base_url=BASE_URL, follow_redirects=True, timeout=20,
        headers={"User-Agent": "NargileSaaS-CatalogImporter/1.0"},
    ) as client:
        for _cat_name, cat_slug, urls in CATEGORIES_TO_SCRAPE:
            for url_path in urls:
                try:
                    products = await scrape_category_page(client, url_path, cat_slug)
                    for p in products:
                        if p.base_code not in seen_codes:
                            seen_codes.add(p.base_code)
                            all_products.append(p)
                except httpx.HTTPError as exc:
                    print(f"  ! Failed to fetch {url_path}: {exc}")

    return all_products


# ── Database seeder ──────────────────────────────────────────────────────────

async def seed(products: list[ScrapedProduct]) -> None:
    async with async_session() as session:
        # Load segments
        result = await session.execute(select(CustomerSegment))
        seg_map: dict[SegmentType, int] = {
            s.code: s.id for s in result.scalars().all()
        }
        if len(seg_map) < 4:
            print("ERROR: Run seed_initial_data.py first to create segments.")
            return

        # Ensure categories exist
        cat_map: dict[str, int] = {}
        for cat_name, cat_slug, _ in CATEGORIES_TO_SCRAPE:
            existing = await session.execute(
                select(Category).where(Category.slug == cat_slug)
            )
            cat = existing.scalar_one_or_none()
            if cat:
                cat_map[cat_slug] = cat.id
            else:
                cat = Category(name=cat_name, slug=cat_slug)
                session.add(cat)
                await session.flush()
                cat_map[cat_slug] = cat.id
        await session.commit()

        # Seed products
        stats = {"products": 0, "variants": 0, "prices": 0, "skipped": 0}

        for sp in products:
            # Check if base_code already exists
            existing = await session.execute(
                select(Product).where(Product.base_code == sp.base_code)
            )
            if existing.scalar_one_or_none():
                stats["skipped"] += 1
                continue

            product = Product(
                base_code=sp.base_code,
                name=sp.name,
                brand=None,
                description=None,
                category_id=cat_map.get(sp.category_slug),
                tags=sp.tags,
            )
            session.add(product)
            await session.flush()
            stats["products"] += 1

            # Create single base variant
            sku = make_sku(sp.base_code)
            variant = ProductVariant(
                product_id=product.id,
                sku=sku,
                attributes={"source": "nargilemalzemesi.com"},
            )
            session.add(variant)
            await session.flush()
            stats["variants"] += 1

            # 4-tier pricing matrix
            for seg_type, multiplier in TIER_MULTIPLIERS.items():
                excl = (sp.price_excl_tax * multiplier).quantize(Decimal("0.01"))
                session.add(VariantPrice(
                    variant_id=variant.id,
                    segment_id=seg_map[seg_type],
                    currency=CurrencyType.TRY,
                    price_excl_tax=excl,
                    price_incl_tax=incl_tax(excl),
                    vat_rate=VAT_RATE,
                ))
                stats["prices"] += 1

            # Dual-warehouse inventory
            session.add(Inventory(
                variant_id=variant.id,
                warehouse_type=WarehouseType.RETAIL_STOCK,
                quantity=RETAIL_STOCK_QTY,
            ))
            session.add(Inventory(
                variant_id=variant.id,
                warehouse_type=WarehouseType.B2B_STOCK,
                quantity=B2B_STOCK_QTY,
            ))

            tag_label = "  [HEAVY_CARGO]" if sp.tags else ""
            print(
                f"  + {sp.base_code:<20s} {sp.name[:50]:<50s} "
                f"RETAIL={str(sp.price_excl_tax):>10s}{tag_label}"
            )

            await session.commit()

    return stats


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("═══ Nargile Live Catalog Scraper ═══\n")

    print("1. Scraping nargilemalzemesi.com ...")
    products = await scrape_all()
    print(f"   Scraped {len(products)} unique products\n")

    if not products:
        print("   No products found. Exiting.")
        return

    print("2. Seeding database ...\n")
    stats = await seed(products)

    print(f"\n═══ Import Summary ═══")
    print(f"  Products created:  {stats['products']}")
    print(f"  Variants created:  {stats['variants']}")
    print(f"  Price records:     {stats['prices']}")
    print(f"  Skipped (exists):  {stats['skipped']}")
    print(f"  Inventory rows:    {stats['variants'] * 2}")
    print(f"═══ Done ═══")


if __name__ == "__main__":
    asyncio.run(main())
