"""Words shoppers search with that the catalogue does not use.

The shop names things as the trade does ("Nargile Şişesi", "Marpuç", "Isı
Yönetim Araçları"), while many buyers type what they see or what a friend
calls it ("yedek cam", "nargile hortumu", "hmd"): on Google, and in the shop's
own search, which matches names. A synonym widens a search to whole categories
without renaming a single product: the query still finds what its words
match, and those categories' products as well.

Each rule: a word the query must contain (as a whole word, so "lastik" is not
"plastik"), words that rule it out (the query is about something else), and
the categories it also means. Rules only ever add: a specific search is never
narrowed. Queries arrive folded (Turkish letters to ASCII, lower case:
"Nargile Hortumu" → "nargile hortumu", "lastiği" → "lastigi").

Categories are by slug and must exist and be live (app.api.v1.products checks
nothing more): one renamed or retired here simply adds nothing.
"""

import re
from dataclasses import dataclass

from sqlalchemy import select

from app.models.catalog import Category


def _word(pattern: str) -> re.Pattern[str]:
    """`pattern` as a whole word (or words) in a folded query."""
    return re.compile(rf"(?<![0-9a-z])(?:{pattern})(?![0-9a-z])")


@dataclass(frozen=True)
class Synonym:
    wants: re.Pattern[str]
    unless: re.Pattern[str] | None
    categories: tuple[str, ...]


SYNONYMS: tuple[Synonym, ...] = (
    # "yedek cam", "nargile camı": a hookah's glass is its şişe — unless the
    # glass is a bowl's (a cam lüle, Samsaris's inner chamber).
    Synonym(_word(r"cam(?:i|lar|lari)?"), re.compile(r"lule|hazne|samsaris|bowl"), ("siseler",)),
    # "nargile hortumu": the marpuç — not its spring ("hortum yayı").
    Synonym(_word(r"hortum(?:u|lar|lari)?"), re.compile(r"yay"), ("marpuclar",)),
    # "ağızlık": the sipsi.
    Synonym(_word(r"agizlik(?:i|lar|lari)?|agizligi"), None, ("ozel-sipsiler", "plastik-ve-slikon-sipsiler")),
    # "hmd", "kaloud", "lotus", "ısı yönetimi": the heat management shelf ("Isı Yönetim Araçları").
    Synonym(_word(r"hmd|kaloud|lotus|isi yonetim(?:i)?"), None, ("kozlukler",)),
    # "şişe lastiği", "lüle lastiği": the contalar.
    Synonym(_word(r"lastik(?:leri|lari|i|gi)?|lastigi"), None, ("contalar",)),
    # "nargile kafası", "başlık": the lüle — not a marpuç's head ("marpuç başlığı").
    Synonym(_word(r"kafa(?:si|lar|lari)?|baslik(?:lar|lari)?|basligi"), re.compile(r"marpuc"), ("luleler",)),
    # "köz makinesi", "kömür yakıcı", "köz ocağı": the coal heaters.
    Synonym(
        _word(r"(?:koz|komur) (?:makine(?:si)?|yakici|ocag(?:i)?|ocak)"),
        None,
        ("elektrikli-ocaklar", "gazli-ocaklar"),
    ),
)


def synonym_categories(folded_query: str) -> list[str]:
    """The category slugs a (folded) query also means; empty for most."""
    found: list[str] = []
    for rule in SYNONYMS:
        if rule.wants.search(folded_query) and not (rule.unless and rule.unless.search(folded_query)):
            found.extend(slug for slug in rule.categories if slug not in found)
    return found


def squashed(folded_query: str) -> str:
    """The query without spaces and punctuation: "mr wood", "mr.wood" and
    "mrwood" all become "mrwood", as a name squashed the same way does
    ("Mr.Wood" → "mrwood", "Black Coco's" → "blackcocos")."""
    return re.sub(r"[^0-9a-z]", "", folded_query)


def synonym_subtree(slugs: list[str]):
    """Ids of these categories and their descendants. A CTE of its own name, so
    a search inside a category (whose filter is `category_subtree`'s
    "category_tree") can carry it in the same statement."""
    tree = select(Category.id).where(Category.slug.in_(slugs)).cte("synonym_tree", recursive=True)
    tree = tree.union_all(select(Category.id).where(Category.parent_id == tree.c.id))
    return select(tree.c.id)
