"""URL slugs, shared by the models (a product's own) and the admin schemas."""

import re

_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# A product's slug: lowercase letters and digits in runs joined by single
# hyphens or dots. The dot is there for the old store's addresses, which kept
# brand names as written ("mr.wood-nargile-takimi-…") and are kept as they are,
# so a link to the old store lands on the same product here.
PRODUCT_SLUG = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
PRODUCT_SLUG_MAX = 255


def slugify(text: str) -> str:
    """Turkish-aware URL slug: "Çelik Takımlar" → "celik-takimlar"."""
    ascii_text = text.translate(_TR).lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def product_slug_base(name: str) -> str:
    """The slug a new product's name gives it, before any "-2" to keep it unique."""
    # Room for a "-NNN" suffix within the column.
    return slugify(name)[: PRODUCT_SLUG_MAX - 8].strip("-") or "urun"
