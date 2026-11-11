"""Words shoppers search with that the catalogue does not use.

The shop names things as the trade does ("Nargile Şişesi"), while many buyers
type what they see ("yedek cam", "nargile camı"): on Google, and in the shop's
own search, which matches names. A synonym here widens a search to a whole
category without renaming a single product: the query still finds what its
words match, and finds that category's products as well.

Each rule is a word the folded query must contain, words that rule it out
(the query is about something else made of glass), and the category it also
means. Queries arrive folded (Turkish letters to ASCII, lower case:
"Nargile Camı" → "nargile cami").
"""

import re

from sqlalchemy import select

from app.models.catalog import Category

#: (the query says, unless it says, it also means this category).
SYNONYMS: tuple[tuple[re.Pattern[str], re.Pattern[str], str], ...] = (
    # "cam", "camı", "camlar", "camları" — a hookah's glass is its şişe, unless
    # the glass is a bowl's (a cam lüle, Samsaris's inner chamber).
    (
        re.compile(r"(?<![0-9a-z])cam(i|lar|lari)?(?![0-9a-z])"),
        re.compile(r"lule|hazne|samsaris|bowl"),
        "siseler",
    ),
)


def synonym_categories(folded_query: str) -> list[str]:
    """The category slugs a (folded) query also means; empty for most."""
    return [slug for wants, unless, slug in SYNONYMS if wants.search(folded_query) and not unless.search(folded_query)]


def synonym_subtree(slugs: list[str]):
    """Ids of these categories and their descendants. A CTE of its own name, so
    a search inside a category (whose filter is `category_subtree`'s
    "category_tree") can carry it in the same statement."""
    tree = select(Category.id).where(Category.slug.in_(slugs)).cte("synonym_tree", recursive=True)
    tree = tree.union_all(select(Category.id).where(Category.parent_id == tree.c.id))
    return select(tree.c.id)
