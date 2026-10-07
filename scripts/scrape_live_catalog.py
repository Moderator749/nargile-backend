"""Scrape live product catalog from nargilemalzemesi.com and seed the database.

Fetches real products across key categories, creates the 4-tier pricing
matrix (RETAIL / CAFE / WHOLESALE / DEALER), and seeds dual-warehouse
inventory (RETAIL_STOCK + B2B_STOCK).

Idempotent: products whose base_code (site product code) already exists are not
re-created, but their image_url is refreshed from the live site.

    python scripts/scrape_live_catalog.py                # import new + backfill images
    python scripts/scrape_live_catalog.py --images-only  # only backfill image_url
"""

import asyncio
import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from bs4 import BeautifulSoup, Tag
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
    image_url: str | None = None


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


# Lazy-load placeholder the site puts in <img src> before JS swaps the real image in.
PLACEHOLDER_IMAGE = re.compile(r"/transparent-\d+x\d+\.", re.IGNORECASE)
IMAGE_URL_MAX = 500


def extract_image_url(card: Tag) -> str | None:
    """Highest-resolution product image on a listing card, as an absolute URL.

    Cards lazy-load: the real URLs sit in data-srcset ("<400px> 1x, <800px> 2x")
    and data-src, while src holds a transparent placeholder.
    """
    img = card.select_one(".image img")
    if img is None:
        return None
    candidates: list[str] = []
    srcset = img.get("data-srcset") or img.get("srcset") or ""
    # Last srcset entry is the largest (2x).
    candidates += [part.split()[0] for part in reversed(srcset.split(",")) if part.strip()]
    candidates += [img.get("data-src") or "", img.get("src") or ""]
    for raw in candidates:
        if not raw.strip():
            continue
        url = urljoin(BASE_URL + "/", raw.strip())
        if (
            url.startswith("https://")
            and not PLACEHOLDER_IMAGE.search(url)
            and len(url) <= IMAGE_URL_MAX
            and not re.search(r"[\s\"'()<>\\]", url)
        ):
            return url
    return None


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
            image_url=extract_image_url(card),
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

async def backfill_images(products: list[ScrapedProduct]) -> dict[str, int]:
    """Set image_url on products that already exist, matched by base_code."""
    stats = {"updated": 0, "unchanged": 0, "missing": 0, "no_image": 0}
    by_code = {p.base_code: p for p in products}
    async with async_session() as session:
        result = await session.execute(
            select(Product).where(Product.base_code.in_(list(by_code)))
        )
        found = {p.base_code: p for p in result.scalars().all()}
        for code, sp in by_code.items():
            product = found.get(code)
            if product is None:
                stats["missing"] += 1
            elif not sp.image_url:
                stats["no_image"] += 1
            elif product.image_url == sp.image_url:
                stats["unchanged"] += 1
            else:
                product.image_url = sp.image_url
                stats["updated"] += 1
                print(f"  ~ {code:<20s} {sp.image_url}")
        await session.commit()
    return stats


async def seed(products: list[ScrapedProduct]) -> dict[str, int] | None:
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
                image_url=sp.image_url,
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

async def main(images_only: bool = False):
    print("═══ Nargile Live Catalog Scraper ═══\n")

    print("1. Scraping nargilemalzemesi.com ...")
    products = await scrape_all()
    print(f"   Scraped {len(products)} unique products\n")

    if not products:
        print("   No products found. Exiting.")
        return

    with_image = sum(1 for p in products if p.image_url)
    print(f"   {with_image}/{len(products)} have a product image\n")

    if images_only:
        print("2. Backfilling product images ...\n")
        img = await backfill_images(products)
        print("\n═══ Image Summary ═══")
        print(f"  Updated:           {img['updated']}")
        print(f"  Already current:   {img['unchanged']}")
        print(f"  Not in database:   {img['missing']}")
        print(f"  No image on site:  {img['no_image']}")
        print("═══ Done ═══")
        return

    print("2. Seeding database ...\n")
    stats = await seed(products)
    if stats is None:
        return
    img = await backfill_images(products)

    print(f"\n═══ Import Summary ═══")
    print(f"  Products created:  {stats['products']}")
    print(f"  Variants created:  {stats['variants']}")
    print(f"  Price records:     {stats['prices']}")
    print(f"  Skipped (exists):  {stats['skipped']}")
    print(f"  Inventory rows:    {stats['variants'] * 2}")
    print(f"  Images refreshed:  {img['updated']}")
    print(f"═══ Done ═══")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--images-only", action="store_true",
        help="only set image_url on existing products; create nothing",
    )
    asyncio.run(main(images_only=parser.parse_args().images_only))
