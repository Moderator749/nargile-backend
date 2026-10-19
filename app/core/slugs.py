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
