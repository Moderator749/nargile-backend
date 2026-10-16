"""OpenCart → Sultan Nargile catalogue migration tool.

Copies every product of an OpenCart store — name, product code, description,
photos, price, category — into this application's database. Written for the
shop's old store, https://www.nargilemalzemesi.com (OpenCart 3, "Journal"-style
"opencartvip3" theme), and kept so it can be run again (to pick up products
added to the old store) or pointed at another OpenCart site.

────────────────────────────────────────────────────────────────────────────
RUNNING IT
────────────────────────────────────────────────────────────────────────────
From the backend folder, with the virtualenv's Python and the database in
.env reachable:

    cd saas_backend
    .venv/bin/python scripts/opencart_migration_tool.py --dry-run --limit 5   # look, write nothing
    .venv/bin/python scripts/opencart_migration_tool.py --limit 20            # a small real batch
    .venv/bin/python scripts/opencart_migration_tool.py                       # everything

It prints one line per product ("+ created", "~ updated", "= unchanged",
"! failed") and a summary at the end, and writes the same as JSON to
--report (default: scripts/migration_reports/opencart-<date>.json).

Useful options (all optional):
    --base-url URL        the OpenCart store (default: BASE_URL below)
    --limit N             only the first N products found
    --only URL [URL …]    only these product pages
    --from-category URL   only the products listed on this category page of
                          the old store (every ?page=N of it)
    --category SLUG       put every product in this local category (as its
                          primary one on a new product; an existing product
                          keeps its categories and is added to this one too),
                          instead of CATEGORY_MAP / NAME_RULES
    --brand NAME          the brand of every product, whatever the page says
                          (matched to this application's brand list ignoring
                          spaces and punctuation: "Mr. Wood" → Mr.Wood).
                          Without it, each product gets the brand of this
                          application's list that its page's brand, or else its
                          name, mentions ("Legend Hookah" → Legend,
                          "Steamulation Pure …" → Steamulation); no match keeps
                          the page's brand, and no brand at all is "Markasız"
    --dry-run             scrape and report, write nothing (no DB, no files)
    --publish             new products go live at once (default: drafts)
    --stock N             stock for new in-stock products (default: 0)
    --update-prices       also overwrite the retail price of existing products
    --overwrite           also overwrite name/description/category/brand of
                          existing products (default: only fill what is empty)
    --keep-html           store descriptions as HTML (default: clean text —
                          the storefront shows descriptions as text)
    --skip-images         do not download photos
    --media-base-url URL  public address of this API, which serves /media
                          (default: http://127.0.0.1:8000; on the live server,
                          its https address, e.g. https://api.example.com)
    --concurrency N       parallel page fetches (default: 4; be polite)

────────────────────────────────────────────────────────────────────────────
WHAT IT DOES, AND WHAT IT LEAVES ALONE
────────────────────────────────────────────────────────────────────────────
1. Discovery. Reads the store's sitemap index (SITEMAP_PATH) and every
   "sitemap-product-*.xml" in it. A store without one is crawled instead:
   each category page in the top menu, following ?page=N until it is empty.
2. Each product page is read (see SELECTORS):
   - name, product code ("model"), price, stock status and brand from the
     page's schema.org JSON-LD Product block — the most stable source, the
     same on most OpenCart themes — with the visible elements as fallback;
   - the description from the description block, as text or HTML;
   - photos from the gallery: the ORIGINAL upload (/image/catalog/…), not the
     resized cache copy (/image/cache/catalog/…-1000x1000h.jpg), when the
     original exists;
   - the category from the breadcrumb's last category link.
3. Matching, so it can run any number of times (idempotent):
   by product code (case-insensitive) against Product.base_code, then against
   a colour's SKU, then by exact name — the name only when that product's
   code is not another page's on the old store, where one name often covers
   several codes. One match → update; none → create.
   All pages are read before anything is written, because the old store
   sometimes gives two different products (colours of one model) the same
   code: sorted by address, the first keeps the code and the others become
   CODE-2, CODE-3… — never merged into each other, and the same on a re-run.
4. Creating a product: one variant (SKU = the code), the RETAIL price row
   (VAT-inclusive, VAT_RATE), and one stock row per warehouse. New products
   are DRAFTS (unpublished) with zero stock unless --publish / --stock: the
   old store shows no quantities and no B2B prices, so a person sets those
   (Ürünler & Stok, Fiyat Hesaplama Cetveli) and publishes.
5. Updating a product: photos that are missing or still point at the old
   store are replaced by local copies (the old store will be switched off, so
   its links would break); everything else is only filled in where empty,
   unless --overwrite / --update-prices say otherwise. Prices and categories
   the team has set in the new system are never touched by default.
6. Photos are saved under MEDIA_ROOT/products/opencart/ with a name derived
   from the source address, so a re-run reuses the file instead of fetching
   it again, and stored as <media-base-url>/media/products/opencart/<file>.
7. Each product is its own transaction: one failure is reported and rolled
   back, and the run goes on.

────────────────────────────────────────────────────────────────────────────
ADAPTING IT TO ANOTHER OPENCART SITE
────────────────────────────────────────────────────────────────────────────
- BASE_URL, and SITEMAP_PATH if the sitemap lives elsewhere (stock OpenCart:
  index.php?route=extension/feed/google_sitemap).
- SELECTORS: CSS selectors for the theme's elements. Stock OpenCart 3
  ("default" theme) equivalents are noted beside each.
- CATEGORY_MAP: the old store's category URL (last path segment) → this
  application's category slug. Unmapped categories fall back to NAME_RULES
  (keywords in the product name), then to no category; the summary lists
  every unmapped category so the table can be completed.
- SKIP_CATEGORIES: old categories whose products are never imported at all
  (tobacco: selling it online is not permitted in Turkey). Their pages are
  reported as "skipped" and their photos are not downloaded.
- VAT_RATE: the rate the old store's prices include.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup, NavigableString, Tag  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import async_session, engine  # noqa: E402
from app.core.rich_text import sanitize_html, to_rich_html  # noqa: E402
from app.core.slugs import PRODUCT_SLUG, PRODUCT_SLUG_MAX  # noqa: E402
from app.models.brand import Brand  # noqa: E402
from app.models.catalog import (  # noqa: E402
    Category,
    CurrencyType,
    Inventory,
    Product,
    ProductCategory,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.user import CustomerSegment, SegmentType  # noqa: E402
from app.services.media import InvalidImage, image_extension  # noqa: E402

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = "https://www.nargilemalzemesi.com"
SITEMAP_PATH = "/sitemap.xml"  # a sitemap index; product sitemaps match PRODUCT_SITEMAP
PRODUCT_SITEMAP = re.compile(r"sitemap-product[^/]*\.xml$")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) SultanNargile-Migration/1.0"
REQUEST_TIMEOUT = 30
RETRIES = 3

# The old store's prices are what customers pay; this is the VAT inside them.
VAT_RATE = Decimal("20")

# Where photos go: MEDIA_ROOT/<MEDIA_FOLDER>/, served at /media/<MEDIA_FOLDER>/.
MEDIA_FOLDER = "products/opencart"
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# CSS selectors for the "opencartvip3" (Journal-style) theme. Stock OpenCart 3
# equivalents in the comments. JSON-LD is read first; these are the fallback.
SELECTORS = {
    "title": "h1",                                       # stock: #content h1
    "code": "li.product-model",                          # stock: ul.list-unstyled li (Model:)
    "price": ".product-price, .product-price-new",       # stock: ul.list-unstyled h2
    "description": "div.block-description",              # stock: #tab-description
    "gallery": ".product-image img[data-largeimg]",      # stock: ul.thumbnails a.thumbnail (href)
    "breadcrumb": "ul.breadcrumb a",                     # stock: ul.breadcrumb a
    # Category crawl fallback (only when there is no sitemap):
    "menu_category": "#main-menu a, .main-menu a, nav a",
    "listing_product": ".product-thumb .name a, .product-layout .name a",
}

# Old category (last URL segment) → this application's category slug.
CATEGORY_MAP: dict[str, str] = {
    "paslanmaz-celik-nargile-takimlari": "celik-takimlar",
    "legend-hookah-nargile-takimlari": "celik-takimlar",
    "oduman-nargile-takimlari": "nargile-takimlari",
    "wd-hookah-nargile-takimlari": "nargile-takimlari",
    "meduse-experience-hookah-nargile-takimi": "nargile-takimlari",
    "mr-wood-nargile-takimlari": "nargile-takimlari",
    "sultan-nargile-takimlari-ve-malzemeleri": "nargile-takimlari",
    "ithal-nargile": "nargile-takimlari",
    "dokum-nargile-takimlari": "klasik-takimlar",
    "rus-nargile-takimlari-ve-modelleri": "rus-takimlari",
    "masaustu-ve-led-isikli-nargile-takimlari": "mini-masaustu-takimlar",
    "nargile-siseleri": "yedek-sise-ve-govde",
    "nargile-komuru": "nargile-komurleri",
    "nargile-lule-cesitleri": "luleler",
    "kozlukler": "kozlukler",
    "nargile-marpuclari": "marpuc-ve-hortumlar",
    "plastik-ve-ozel-sipsiler": "sipsiler",
    "yedek-parca": "conta-ve-yedek-parca",
    "nargile-malzemeleri-ve-aksesuarlari": "sipsiler-ve-aksesuarlar",
    "diger": "sipsiler-ve-aksesuarlar",
    "nargile-tutunu-markalari-ve-cesitleri": "tutun-bitkisel-ve-sarf",
    "okey-tavla-ve-iskambil-kartlari": "oyun-ve-eglence",
    "kampanyalar": "kampanyalar",
    # "nargile-serleri": left to NAME_RULES / no category.
}

# When the breadcrumb's category is unmapped: the first rule whose keyword is
# in the (lower-cased, Turkish-folded) product name decides.
NAME_RULES: list[tuple[str, str]] = [
    ("kömür", "nargile-komurleri"),
    ("komur", "nargile-komurleri"),
    ("lüle", "luleler"),
    ("lule", "luleler"),
    ("marpuç", "marpuc-ve-hortumlar"),
    ("hortum", "marpuc-ve-hortumlar"),
    ("maşa", "kozlukler"),
    ("közlük", "kozlukler"),
    ("şişe", "yedek-sise-ve-govde"),
    ("sipsi", "sipsiler"),
    ("conta", "conta-ve-yedek-parca"),
    ("tütün", "tutun-bitkisel-ve-sarf"),
    ("okey", "oyun-ve-eglence"),  # before "takım": "okey takımı" is a game
    ("tavla", "oyun-ve-eglence"),
    ("iskambil", "oyun-ve-eglence"),
    ("kampanya", "kampanyalar"),
    ("takım", "nargile-takimlari"),
]

# Never published by this tool, whatever --publish says.
SKIP_CATEGORIES = {"nargile-tutunu-markalari-ve-cesitleri"}

WAREHOUSES = (
    WarehouseType.RETAIL_STOCK,
    WarehouseType.CAFE_STOCK,
    WarehouseType.B2B_STOCK,
    WarehouseType.DEALER_STOCK,
)

log = logging.getLogger("opencart-migration")

# ── Scraping ─────────────────────────────────────────────────────────────────


@dataclass
class ScrapedProduct:
    url: str
    code: str
    name: str
    brand: str | None
    price: Decimal | None  # VAT-inclusive retail
    in_stock: bool
    description: str | None
    images: list[str] = field(default_factory=list)  # source URLs, best first
    category_path: str | None = None  # the old store's, e.g. "diger/nargile-komuru"


class Fetcher:
    """Polite HTTP: a few requests at a time, retried with backoff."""

    def __init__(self, concurrency: int):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True
        )
        self.gate = asyncio.Semaphore(concurrency)

    async def get(self, url: str) -> httpx.Response | None:
        for attempt in range(1, RETRIES + 1):
            async with self.gate:
                try:
                    resp = await self.client.get(url)
                    if resp.status_code == 200:
                        return resp
                    if resp.status_code == 404:
                        return None
                except httpx.HTTPError as exc:
                    log.debug("fetch %s failed (%s), attempt %d", url, exc, attempt)
            await asyncio.sleep(1.5 * attempt)
        return None

    async def close(self) -> None:
        await self.client.aclose()


async def discover(fetcher: Fetcher, base: str) -> list[str]:
    """Every product page: from the sitemap index, else by crawling categories."""
    index = await fetcher.get(urljoin(base, SITEMAP_PATH))
    urls: list[str] = []
    if index is not None:
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", index.text)
        product_maps = [u.replace("&amp;", "&") for u in locs if PRODUCT_SITEMAP.search(u)]
        if not product_maps and "<urlset" in index.text:
            product_maps = []
            urls = [u.replace("&amp;", "&") for u in locs]  # a plain sitemap
        for sm in product_maps:
            resp = await fetcher.get(sm)
            if resp is not None:
                urls += [u.replace("&amp;", "&") for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", resp.text)]
    if not urls:
        log.info("No product sitemap; crawling categories instead.")
        urls = await crawl_categories(fetcher, base)
    return list(dict.fromkeys(urls))  # order kept, duplicates dropped


async def crawl_categories(fetcher: Fetcher, base: str) -> list[str]:
    home = await fetcher.get(base)
    if home is None:
        return []
    soup = BeautifulSoup(home.text, "html.parser")
    host = urlparse(base).netloc
    categories = {
        urljoin(base, a["href"]).split("?")[0]
        for a in soup.select(SELECTORS["menu_category"])
        if a.get("href") and urlparse(urljoin(base, a["href"])).netloc == host
    }
    found: list[str] = []
    for cat in sorted(categories):
        for page in range(1, 200):
            resp = await fetcher.get(f"{cat}?limit=100&page={page}")
            if resp is None:
                break
            links = [urljoin(base, a["href"]) for a in BeautifulSoup(resp.text, "html.parser").select(SELECTORS["listing_product"]) if a.get("href")]
            new = [u for u in links if u not in found]
            if not new:
                break
            found += new
    return found


async def category_listing(fetcher: Fetcher, url: str, base: str) -> list[str]:
    """The product pages one category page lists, over all its pages, in order."""
    found: list[str] = []
    for page in range(1, 200):
        resp = await fetcher.get(f"{url.split('?')[0]}?limit=100&page={page}")
        if resp is None:
            break
        links = [
            urljoin(base, a["href"]).split("?")[0]
            for a in BeautifulSoup(resp.text, "html.parser").select(SELECTORS["listing_product"])
            if a.get("href")
        ]
        new = [u for u in dict.fromkeys(links) if u not in found]
        if not new:
            break
        found += new
    return found


def _money(text: str | None) -> Decimal | None:
    """'2.649,00TL' / '2649.00' → Decimal('2649.00')."""
    if not text:
        return None
    t = re.sub(r"[^\d.,]", "", text)
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if value > 0 else None


def _json_ld_product(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data.get("@graph", [data]) if isinstance(data, dict) else data:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return {}


def _rich(description: str | None, keep_html: bool) -> str | None:
    if not description:
        return None
    return (sanitize_html(description) or None) if keep_html else to_rich_html(description)


def _description(block: Tag | None, keep_html: bool) -> str | None:
    """The description block, as clean text (paragraphs, line breaks and list
    bullets kept) or, with keep_html, its inner HTML minus scripts and styles."""
    if block is None:
        return None
    for junk in block.find_all(["script", "style", "iframe", "noscript"]):
        junk.decompose()
    if keep_html:
        html = block.decode_contents().strip()
        return html or None

    lines: list[str] = []
    current: list[str] = []

    def flush():
        text = re.sub(r"[ \t ]+", " ", "".join(current)).strip()
        if text:
            lines.append(text)
        current.clear()

    def walk(node):
        if isinstance(node, NavigableString):
            current.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            flush()
            return
        block_level = node.name in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "table"}
        if block_level:
            flush()
        if node.name == "li":
            current.append("• ")
        for child in node.children:
            walk(child)
        if block_level:
            flush()

    walk(block)
    flush()
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _original_image(url: str) -> str:
    """'/image/cache/catalog/x/y-1000x1000h.jpg' → '/image/catalog/x/y.jpg'."""
    return re.sub(r"/image/cache/(catalog/.+?)-\d+x\d+[a-z]?(\.\w+)$", r"/image/\1\2", url)


def parse_product(url: str, html: str, base: str, keep_html: bool) -> ScrapedProduct | None:
    soup = BeautifulSoup(html, "html.parser")
    ld = _json_ld_product(soup)

    name = (ld.get("name") or "").strip()
    if not name and (h1 := soup.select_one(SELECTORS["title"])):
        name = h1.get_text(" ", strip=True)
    code = (ld.get("model") or ld.get("sku") or ld.get("mpn") or "").strip()
    if not code and (el := soup.select_one(SELECTORS["code"])):
        code = el.get_text(" ", strip=True).split(":")[-1].strip()
    if not name or not code:
        return None  # not a product page (or a product without a code to match on)

    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = _money(str(offers.get("price") or "")) or _money(
        (soup.select_one(SELECTORS["price"]) or Tag(name="x")).get_text()
    )
    in_stock = "InStock" in str(offers.get("availability", "")) or "Stokta var" in soup.get_text()
    brand = ld.get("brand", {}).get("name") if isinstance(ld.get("brand"), dict) else ld.get("brand")

    gallery = [img.get("data-largeimg") for img in soup.select(SELECTORS["gallery"]) if img.get("data-largeimg")]
    if not gallery and ld.get("image"):
        gallery = ld["image"] if isinstance(ld["image"], list) else [ld["image"]]
    images = list(dict.fromkeys(urljoin(base, u) for u in gallery))

    crumbs = [a.get("href", "") for a in soup.select(SELECTORS["breadcrumb"])]
    host = urlparse(base).netloc
    category_paths = [
        urlparse(h).path.strip("/") for h in crumbs[1:-1] if urlparse(urljoin(base, h)).netloc == host
    ]
    return ScrapedProduct(
        url=url,
        code=code[:30],
        name=re.sub(r"\s+", " ", name)[:255],
        brand=(brand or None) and str(brand)[:100],
        price=price,
        in_stock=in_stock,
        # Stored as the editor's HTML: cleaned markup, or the text as paragraphs.
        description=_rich(_description(soup.select_one(SELECTORS["description"]), keep_html), keep_html),
        images=images,
        category_path=category_paths[-1] if category_paths else None,
    )


# ── Photos ───────────────────────────────────────────────────────────────────


async def store_images(fetcher: Fetcher, sources: list[str], media_base: str, dry_run: bool) -> tuple[list[str], int]:
    """Local copies of the photos (original upload preferred over the resized
    cache copy), named after their source so a re-run reuses them. Returns the
    public URLs and how many files were newly downloaded."""
    urls: list[str] = []
    downloaded = 0
    for src in sources:
        candidates = list(dict.fromkeys([_original_image(src), src]))
        name = hashlib.sha1(candidates[0].encode()).hexdigest()[:24]
        folder = settings.MEDIA_ROOT / MEDIA_FOLDER
        existing = next(iter(folder.glob(f"{name}.*")), None) if folder.exists() else None
        if existing is not None:
            urls.append(f"{media_base}/media/{MEDIA_FOLDER}/{existing.name}")
            continue
        if dry_run:
            urls.append(candidates[0])
            continue
        for candidate in candidates:
            resp = await fetcher.get(candidate)
            if resp is None or not resp.content or len(resp.content) > MAX_IMAGE_BYTES:
                continue
            try:
                ext = image_extension(resp.content)
            except InvalidImage:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{name}{ext}").write_bytes(resp.content)
            urls.append(f"{media_base}/media/{MEDIA_FOLDER}/{name}{ext}")
            downloaded += 1
            break
    return urls, downloaded


# ── Database ─────────────────────────────────────────────────────────────────


def _fold(text: str) -> str:
    return text.translate(str.maketrans("İIı", "iii")).lower()


def map_category(p: ScrapedProduct, categories: dict[str, int]) -> tuple[int | None, str | None]:
    """(category id, the old category if it was not in CATEGORY_MAP)."""
    old = p.category_path.split("/")[-1] if p.category_path else None
    slug = CATEGORY_MAP.get(old) if old else None
    unmapped = old if old and not slug else None
    if not slug:
        folded = _fold(p.name)
        slug = next((s for kw, s in NAME_RULES if _fold(kw) in folded), None)
    return categories.get(slug) if slug else None, unmapped


def resolve_brand(p: ScrapedProduct, brands: list[tuple[str, list[str]]]) -> str | None:
    """The local brand the page's brand, or else the product name, mentions as a
    whole word; else the page's brand as it is (None: no brand)."""
    for text in (p.brand, p.name):
        if not text:
            continue
        words = f" {re.sub(r'[^0-9a-z.]+', ' ', _fold(text))} "
        for name, terms in brands:
            if any(f" {term} " in words for term in terms):
                return name
    return p.brand


def local_brand(given: str, brands: list[tuple[str, list[str]]]) -> str:
    """The brand list's spelling of `given`, compared on letters and digits only
    ("Mr. Wood" is Mr.Wood); `given` itself when the list has no such brand."""
    key = re.sub(r"[^0-9a-z]+", "", _fold(given))
    for name, terms in brands:
        if any(re.sub(r"[^0-9a-z]+", "", t) == key for t in terms):
            return name
    return given


def is_old_store_link(url: str | None, base: str) -> bool:
    return bool(url) and urlparse(url).netloc.replace("www.", "") == urlparse(base).netloc.replace("www.", "")


async def find_product(db, p: ScrapedProduct, source_codes: set[str]) -> Product | None:
    code = p.code.strip().lower()
    product = await db.scalar(select(Product).where(func.lower(func.trim(Product.base_code)) == code))
    if product is None:
        pid = await db.scalar(select(ProductVariant.product_id).where(func.lower(ProductVariant.sku) == code))
        product = await db.get(Product, pid) if pid else None
    if product is None:
        # By name only for a product whose code is not another page's on the old
        # store: there, one name often covers several codes (sizes, colours).
        for candidate in (await db.execute(select(Product).where(func.lower(Product.name) == p.name.lower()))).scalars():
            if candidate.base_code.strip().lower() not in source_codes:
                return candidate
    return product


async def legacy_slug(db, url: str) -> str | None:
    """The old store's address for a product (its last path segment), as this
    application's slug — when it is a valid one and no other product has it."""
    slug = urlparse(url).path.strip("/").rsplit("/", 1)[-1].lower()
    if not PRODUCT_SLUG.match(slug) or len(slug) > PRODUCT_SLUG_MAX:
        return None
    if await db.scalar(select(Product.id).where(Product.slug == slug)):
        return None
    return slug


@dataclass
class Outcome:
    url: str
    code: str
    action: str  # created | updated | unchanged | failed | skipped
    changes: list[str] = field(default_factory=list)
    error: str | None = None
    unmapped_category: str | None = None


async def upsert(p: ScrapedProduct, image_urls: list[str], ctx: dict, opts) -> Outcome:
    out = Outcome(url=p.url, code=p.code, action="unchanged")
    if ctx.get("forced_category") is not None:
        category_id = ctx["forced_category"]
    else:
        category_id, out.unmapped_category = map_category(p, ctx["categories"])
    p.brand = local_brand(opts.brand, ctx["brands"]) if opts.brand else resolve_brand(p, ctx["brands"])
    vat_factor = 1 + VAT_RATE / 100

    async with async_session() as db:
        try:
            product = await find_product(db, p, ctx["codes"])
            if product is None:
                out.action = "created"
                product = Product(
                    base_code=p.code, name=p.name, brand=p.brand, description=p.description,
                    category_id=category_id, tags=[], image_url=image_urls[0] if image_urls else None,
                    image_urls=image_urls or None, is_published=opts.publish,
                    # The old store's own address, so its links land here (None
                    # = derived from the name; see Product's slug).
                    slug=await legacy_slug(db, p.url),
                )
                db.add(product)
                await db.flush()
                if category_id is not None:
                    db.add(ProductCategory(product_id=product.id, category_id=category_id, position=0))
                sku = p.code
                if await db.scalar(select(ProductVariant.id).where(func.lower(ProductVariant.sku) == sku.lower())):
                    sku = f"{p.code[:26]}-OC"
                variant = ProductVariant(product_id=product.id, sku=sku)
                db.add(variant)
                await db.flush()
                if p.price is not None:
                    db.add(VariantPrice(
                        variant_id=variant.id, segment_id=ctx["retail_id"], currency=CurrencyType.TRY,
                        price_incl_tax=p.price,
                        price_excl_tax=(p.price / vat_factor).quantize(Decimal("0.01"), ROUND_HALF_UP),
                        vat_rate=VAT_RATE,
                    ))
                for wh in WAREHOUSES:
                    qty = opts.stock if (wh is WarehouseType.RETAIL_STOCK and p.in_stock) else 0
                    db.add(Inventory(variant_id=variant.id, warehouse_type=wh, quantity=qty))
            else:
                ch = out.changes
                # Photos: local copies wherever they are missing or still on the old store.
                if image_urls and (not product.image_url or is_old_store_link(product.image_url, opts.base_url)):
                    product.image_url = image_urls[0]
                    ch.append("image")
                gallery = product.image_urls or []
                if image_urls and (not gallery or all(is_old_store_link(u, opts.base_url) for u in gallery)):
                    product.image_urls = image_urls
                    ch.append("gallery")
                if p.description and (opts.overwrite or not (product.description or "").strip()):
                    if product.description != p.description:
                        product.description = p.description
                        ch.append("description")
                if p.brand and (opts.overwrite or not product.brand) and product.brand != p.brand:
                    product.brand = p.brand
                    ch.append("brand")
                if category_id and (opts.overwrite or product.category_id is None) and product.category_id != category_id:
                    product.category_id = category_id
                    ch.append("category")
                forced = ctx.get("forced_category")
                if forced is not None and product.category_id != forced and not await db.scalar(
                    select(ProductCategory.category_id).where(
                        ProductCategory.product_id == product.id, ProductCategory.category_id == forced
                    )
                ):
                    if product.category_id is None:
                        product.category_id = forced
                    position = await db.scalar(
                        select(func.coalesce(func.max(ProductCategory.position) + 1, 0)).where(
                            ProductCategory.product_id == product.id
                        )
                    )
                    db.add(ProductCategory(product_id=product.id, category_id=forced, position=position))
                    ch.append("category (added)")
                if opts.overwrite and product.name != p.name:
                    product.name = p.name
                    ch.append("name")
                if p.price is not None:
                    await _retail_price(db, product, p.price, ctx, opts, ch)
                out.action = "updated" if ch else "unchanged"

            if opts.dry_run:
                await db.rollback()
            else:
                await db.commit()
        except Exception as exc:  # one product's failure must not end the run
            await db.rollback()
            out.action, out.error = "failed", f"{type(exc).__name__}: {exc}"[:300]
    return out


async def _retail_price(db, product: Product, price: Decimal, ctx: dict, opts, changes: list[str]) -> None:
    """Set the retail price where the product has none; overwrite it only with --update-prices."""
    vat_factor = 1 + VAT_RATE / 100
    variants = (await db.execute(select(ProductVariant).where(ProductVariant.product_id == product.id, ProductVariant.is_active.is_(True)))).scalars().all()
    for variant in variants:
        rows = (await db.execute(select(VariantPrice).where(
            VariantPrice.variant_id == variant.id, VariantPrice.segment_id == ctx["retail_id"]
        ))).scalars().all()
        try_row = next((r for r in rows if CurrencyType(r.currency) == CurrencyType.TRY), None)
        if rows and not opts.update_prices:
            continue
        excl = (price / vat_factor).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if try_row is None:
            db.add(VariantPrice(variant_id=variant.id, segment_id=ctx["retail_id"], currency=CurrencyType.TRY,
                                price_incl_tax=price, price_excl_tax=excl, vat_rate=VAT_RATE))
            changes.append("retail price (new)")
        elif try_row.price_incl_tax != price:
            try_row.price_incl_tax, try_row.price_excl_tax, try_row.vat_rate = price, excl, VAT_RATE
            changes.append(f"retail price {try_row.price_incl_tax}→{price}")


# ── Run ──────────────────────────────────────────────────────────────────────


async def run(opts) -> dict:
    started = time.monotonic()
    fetcher = Fetcher(opts.concurrency)
    base = opts.base_url.rstrip("/")
    try:
        if opts.only:
            urls = opts.only
        elif opts.from_category:
            urls = await category_listing(fetcher, opts.from_category, base)
        else:
            urls = await discover(fetcher, base)
        if opts.limit:
            urls = urls[: opts.limit]
        log.info("%d product pages to read from %s", len(urls), base)

        async with async_session() as db:
            categories = {slug: cid for slug, cid in (await db.execute(select(Category.slug, Category.id))).all()}
            retail_id = await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))
            # Each brand with the words that name it (name, search term, slug),
            # the longest first so "mr.wood" is tried before a shorter term.
            brands = [
                (name, sorted({_fold(t).strip() for t in (name, term, slug.replace("-", " ")) if t and t.strip()}, key=len, reverse=True))
                for name, term, slug in (await db.execute(select(Brand.name, Brand.search_term, Brand.slug))).all()
            ]
            brands.sort(key=lambda b: -len(b[1][0]))
        ctx = {"categories": categories, "retail_id": retail_id, "forced_category": None, "brands": brands}
        if opts.category:
            if opts.category not in categories:
                raise SystemExit(f"No local category with the slug {opts.category!r}")
            ctx["forced_category"] = categories[opts.category]

        outcomes: list[Outcome] = []
        downloaded = 0

        # Phase 1, in parallel: read every page and copy its photos.
        scraped: list[tuple[ScrapedProduct, list[str]]] = []

        async def read(i: int, url: str) -> None:
            nonlocal downloaded
            resp = await fetcher.get(url)
            if resp is None:
                outcomes.append(Outcome(url=url, code="", action="failed", error="page not reachable"))
                log.info("[read %d/%d] ! %s — page not reachable", i, len(urls), url)
                return
            product = parse_product(url, resp.text, base, opts.keep_html)
            if product is None:
                outcomes.append(Outcome(url=url, code="", action="skipped", error="no product name/code on page"))
                log.info("[read %d/%d] - %s — not a product page", i, len(urls), url)
                return
            if product.category_path and product.category_path.split("/")[-1] in SKIP_CATEGORIES:
                outcomes.append(Outcome(url=url, code=product.code, action="skipped", error=f"excluded category {product.category_path}"))
                return
            images, n = ([], 0) if opts.skip_images else await store_images(fetcher, product.images, opts.media_base_url, opts.dry_run)
            downloaded += n
            scraped.append((product, images))
            if i % 50 == 0:
                log.info("[read %d/%d] …", i, len(urls))

        await asyncio.gather(*(read(i, u) for i, u in enumerate(urls, 1)))

        # The old store sometimes gives different products (colours, models) the
        # same code. Sorted by address, the first keeps it and the others get
        # "-2", "-3"…, so none is merged into another and a re-run finds each
        # under the same code again.
        scraped.sort(key=lambda pair: pair[0].url)
        seen: dict[str, int] = {}
        for product, _ in scraped:
            key = product.code.strip().lower()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                suffix = f"-{seen[key]}"
                product.code = product.code.strip()[: 30 - len(suffix)] + suffix

        ctx["codes"] = {product.code.strip().lower() for product, _ in scraped}

        # Phase 2, one product at a time: write to the database.
        for i, (product, images) in enumerate(scraped, 1):
            outcome = await upsert(product, images, ctx, opts)
            outcomes.append(outcome)
            mark = {"created": "+", "updated": "~", "unchanged": "=", "failed": "!"}.get(outcome.action, "?")
            detail = ", ".join(outcome.changes) or outcome.error or ""
            log.info("[%d/%d] %s %-14s %s %s", i, len(scraped), mark, product.code, product.name[:60], f"({detail})" if detail else "")
    finally:
        await fetcher.close()

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.action] = counts.get(o.action, 0) + 1
    unmapped: dict[str, int] = {}
    for o in outcomes:
        if o.unmapped_category:
            unmapped[o.unmapped_category] = unmapped.get(o.unmapped_category, 0) + 1
    return {
        "source": base,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "dry_run": opts.dry_run,
        "counts": counts,
        "images_downloaded": downloaded,
        "unmapped_categories": unmapped,
        "outcomes": [asdict(o) for o in outcomes],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate an OpenCart store's products into this application.")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", nargs="+", metavar="URL")
    parser.add_argument("--from-category", metavar="URL")
    parser.add_argument("--category", metavar="SLUG")
    parser.add_argument("--brand", metavar="NAME")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--stock", type=int, default=0)
    parser.add_argument("--update-prices", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-html", action="store_true")
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--media-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--report", type=Path)
    opts = parser.parse_args()
    opts.media_base_url = opts.media_base_url.rstrip("/")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    engine.echo = False

    report = asyncio.run(run(opts))
    path = opts.report or Path(__file__).parent / "migration_reports" / f"opencart-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    c = report["counts"]
    print("\n── Summary ─────────────────────────────────────────")
    print(f"  source            {report['source']}{'  (DRY RUN: nothing written)' if report['dry_run'] else ''}")
    print(f"  created           {c.get('created', 0)}")
    print(f"  updated           {c.get('updated', 0)}")
    print(f"  unchanged         {c.get('unchanged', 0)}")
    print(f"  skipped           {c.get('skipped', 0)}  (pages without a product, or SKIP_CATEGORIES)")
    print(f"  failed            {c.get('failed', 0)}")
    print(f"  photos downloaded {report['images_downloaded']}")
    print(f"  time              {report['seconds']} s")
    if report["unmapped_categories"]:
        print("  old categories not in CATEGORY_MAP (products placed by name, or left uncategorised):")
        for cat, n in sorted(report["unmapped_categories"].items(), key=lambda kv: -kv[1]):
            print(f"    {cat}: {n}")
    print(f"  report            {path}")


if __name__ == "__main__":
    main()
