"""URL slugs, shared by the models (a product's own) and the admin schemas."""

import re

_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# A product's slug: lowercase letters and digits in runs joined by single
# hyphens or dots. The dot is there for the old store's addresses, which kept
# brand names as written ("mr.wood-nargile-takimi-…") and are kept as they are,
# so a link to the old store lands on the same product here.
PRODUCT_SLUG = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
PRODUCT_SLUG_MAX = 255

# A product's page is at the storefront's root, /<slug>, beside the shop's own
# pages and the old store's addresses the storefront redirects: a product
# given one of these would never be reached. The storefront's pages are
# ROOT_ROUTES in its src/lib/rootRoutes.ts; the old addresses are the root
# redirects of its next.config.ts and src/lib/legacy/categories.ts. A page or
# redirect added there is added here.
RESERVED_ROOT_PATHS = frozenset({
    # The storefront's pages, languages and files.
    "en", "ar", "tr", "api", "_next", "_", "account", "admin", "bilgi", "blog", "cart", "catalog", "checkout",
    "firsat-urunleri", "fiyat-katalogu", "images", "kargo-takip", "login", "magazalar", "marka", "onizleme", "orders",
    "product", "register", "sifre-yenile", "sifremi-unuttum", "sozlesmeler", "sss", "opengraph-image",
    "robots.txt", "sitemap.xml", "llms.txt", "favicon.ico", "icon.png", "apple-icon.png", "index.php", "malzemeler",
    # The old store's shelves and pages, redirected at the root.
    "paslanmaz-celik-nargile-takimlari", "legend-hookah-nargile-takimlari", "oduman-nargile-takimlari",
    "meduse-experience-hookah-nargile-takimi", "mr-wood-nargile-takimlari", "sultan-nargile-takimlari-ve-malzemeleri",
    "dokum-nargile-takimlari", "rus-nargile-takimlari-ve-modelleri", "masaustu-ve-led-isikli-nargile-takimlari",
    "nargile-siseleri", "nargile-komuru", "nargile-lule-cesitleri", "kozlukler", "nargile-marpuclari",
    "plastik-ve-ozel-sipsiler", "nargile-malzemeleri-ve-aksesuarlari", "yedek-parca", "okey-tavla-ve-iskambil-kartlari",
    "nargile-tutunu-markalari-ve-cesitleri", "kampanyalar", "wd-hookah-nargile-takimlari", "diger", "nargile-serleri",
    "nargile-malzemeleri", "rus-nargile-takimlari", "celik-nargile-takimlari", "lule-ve-lule-basliklari", "siseler",
    "oduman", "mr.wood", "legend-hookah", "ozel-sipsiler", "oyun-aletleri", "marpuclar", "wd-hookah",
    "sultan-nargile-takimlari", "masaustu-nargile-takimi", "teslimat-ve-kargolama-bilgileri", "sartlar-ve-kosullar",
    "sikca-sorulan-sorular", "hakkimizda", "gizlilik-sozlesmesi", "ithal-nargile", "legend-hookah-nargile",
    "sultan-nargile",
})


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
