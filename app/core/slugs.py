"""URL slugs, shared by the models (a product's own) and the admin schemas."""

import re

_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# A product's slug: lowercase letters and digits in runs joined by single
# hyphens or dots. The dot is there for the old store's addresses, which kept
# brand names as written ("mr.wood-nargile-takimi-…") and are kept as they are,
# so a link to the old store lands on the same product here.
PRODUCT_SLUG = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
PRODUCT_SLUG_MAX = 255

# Products' and categories' pages are both at the storefront's root, /<slug>,
# beside the shop's own pages and the old addresses the storefront redirects:
# a product or category given one of these would never be reached (and one
# may never take another's: app/services/root_slugs.py). The storefront's
# pages are ROOT_ROUTES in its src/lib/rootRoutes.ts; the old addresses are
# the root redirects of its next.config.ts (LEGACY_PAGES, RETIRED_CATEGORIES)
# and src/lib/legacy/categories.ts. A page or redirect added there is added
# here. An old address that is a live category's slug now (/siseler, /oduman)
# is that category's page, not a redirect, and is not listed.
STOREFRONT_PAGES = frozenset({
    "en", "ar", "tr", "api", "_next", "_", "account", "admin", "bilgi", "blog", "cart", "catalog", "checkout",
    "firsat-urunleri", "fiyat-katalogu", "images", "kargo-takip", "login", "magazalar", "marka", "onizleme", "orders",
    "product", "register", "sifre-yenile", "sifremi-unuttum", "sozlesmeler", "sss", "opengraph-image",
    "robots.txt", "sitemap.xml", "llms.txt", "favicon.ico", "icon.png", "apple-icon.png", "index.php",
})
OLD_ROOT_ADDRESSES = frozenset({
    # The old store's shelves and pages.
    "paslanmaz-celik-nargile-takimlari", "legend-hookah-nargile-takimlari", "oduman-nargile-takimlari",
    "meduse-experience-hookah-nargile-takimi", "mr-wood-nargile-takimlari", "sultan-nargile-takimlari-ve-malzemeleri",
    "dokum-nargile-takimlari", "rus-nargile-takimlari-ve-modelleri", "masaustu-ve-led-isikli-nargile-takimlari",
    "nargile-siseleri", "nargile-komuru", "nargile-lule-cesitleri", "nargile-marpuclari", "plastik-ve-ozel-sipsiler",
    "nargile-malzemeleri-ve-aksesuarlari", "okey-tavla-ve-iskambil-kartlari", "nargile-tutunu-markalari-ve-cesitleri",
    "wd-hookah-nargile-takimlari", "diger", "nargile-serleri", "nargile-malzemeleri", "rus-nargile-takimlari",
    "celik-nargile-takimlari", "lule-ve-lule-basliklari", "mr.wood", "legend-hookah", "oyun-aletleri", "wd-hookah",
    "sultan-nargile-takimlari", "masaustu-nargile-takimi", "teslimat-ve-kargolama-bilgileri", "sartlar-ve-kosullar",
    "sikca-sorulan-sorular", "hakkimizda", "gizlilik-sozlesmesi", "ithal-nargile", "legend-hookah-nargile",
    "sultan-nargile",
    # Category slugs retired by this storefront's taxonomy changes.
    "nargile-komurleri", "hindistan-cevizi-komurleri", "bitkisel-karisimlar", "marpuc-ve-hortumlar",
    "kozlukler-isi-yonetimi", "yedek-siseler", "yedek-sise-ve-govde", "masaustu-nargile-takimlari",
    "mini-masaustu-takimlar", "rus-takimlari", "panada-hookah", "oduman-nargile", "mr-wood-nargile",
    "steamulation-nargile-takimlari", "quasar-arguile", "meduse-nargile-takimlari",
})
RESERVED_ROOT_PATHS = STOREFRONT_PAGES | OLD_ROOT_ADDRESSES


def slugify(text: str) -> str:
    """Turkish-aware URL slug: "Çelik Takımlar" → "celik-takimlar"."""
    ascii_text = text.translate(_TR).lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def product_slug_base(name: str, code: str = "") -> str:
    """The slug a new product gets: its code, then its name — "LH013" and
    "Legend Hookah Çantalı Çelik Nargile Takımı" → "lh013-legend-hookah-cantali-…".

    The code is unique to the product, so two products of one name no longer
    meet at "-2", "-3"; that suffix stays only as a failsafe (see
    _give_product_a_slug). A name that already begins with its code is not
    given it twice.
    """
    name_slug, code_slug = slugify(name), slugify(code)
    if code_slug and name_slug != code_slug and not name_slug.startswith(f"{code_slug}-"):
        base = f"{code_slug}-{name_slug}" if name_slug else code_slug
    else:
        base = name_slug
    # Room for a "-NNN" suffix within the column.
    return base[: PRODUCT_SLUG_MAX - 8].strip("-") or "urun"
