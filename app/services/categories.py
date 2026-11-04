from sqlalchemy import case, select, union

from app.models.catalog import Category, CategoryProductOrder, Product, ProductCategory

#: The menu's depth: Ana grup > Alt kategori > Alt-alt kategori.
MAX_CATEGORY_DEPTH = 3


def ancestors(category_id: int, parents: dict[int, int | None]) -> list[int]:
    """The ids above `category_id`, nearest first. Stops at a loop, which
    would be a data error, rather than walking it for ever."""
    out: list[int] = []
    seen = {category_id}
    up = parents.get(category_id)
    while up is not None and up not in seen:
        out.append(up)
        seen.add(up)
        up = parents.get(up)
    return out


def height(category_id: int, parents: dict[int, int | None]) -> int:
    """Levels from this category down to its deepest descendant: 1 for a leaf."""
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(child)

    def down(node: int, seen: frozenset) -> int:
        below = [down(c, seen | {node}) for c in children.get(node, ()) if c not in seen]
        return 1 + max(below, default=0)

    return down(category_id, frozenset())


def placement_error(category_id: int | None, parent_id: int, parents: dict[int, int | None]) -> str | None:
    """Why `category_id` (None: a new one) may not go under `parent_id`, or None.

    Never under itself or its own branch, and never deeper than the menu
    goes, counting what the category brings along under it."""
    if category_id is not None and (parent_id == category_id or category_id in ancestors(parent_id, parents)):
        return "Bir kategori kendisinin ya da kendi alt kategorisinin altına taşınamaz."
    parent_level = 1 + len(ancestors(parent_id, parents))
    if parent_level >= MAX_CATEGORY_DEPTH:
        return f"Menü en fazla {MAX_CATEGORY_DEPTH} seviye: alt-alt kategorinin altına kategori eklenemez."
    brought = height(category_id, parents) if category_id is not None else 1
    if parent_level + brought > MAX_CATEGORY_DEPTH:
        return (
            f"Bu kategori alt kategorileriyle birlikte buraya taşınırsa menü {MAX_CATEGORY_DEPTH} seviyeyi aşar; "
            "önce alt kategorilerini taşıyın."
        )
    return None


def visible_ids(rows) -> set[int]:
    """Of (id, parent_id, is_active) rows, the ids the storefront shows: active,
    with every category above active too, so switching one off takes its
    whole branch out, however deep."""
    rows = list(rows)
    parents = {id_: parent for id_, parent, _ in rows}
    active = {id_ for id_, _, on in rows if on}
    shown = set()
    for id_ in active:
        chain = ancestors(id_, parents)
        # A loop (a data error) has no top: it shows nowhere.
        if parents.get(chain[-1] if chain else id_) is not None:
            continue
        if all(a in active for a in chain):
            shown.add(id_)
    return shown


def category_subtree(slug: str):
    """Ids of the category with this slug and all its descendants (recursive CTE)."""
    tree = select(Category.id).where(Category.slug == slug).cte("category_tree", recursive=True)
    tree = tree.union_all(select(Category.id).where(Category.parent_id == tree.c.id))
    return select(tree.c.id)


def memberships():
    """(product_id, category_id) for every category a product is listed under:
    its primary one (`products.category_id`) and every row in product_categories.

    Both, because a product written outside the sheet (imports, seeds, the bulk
    move) may only carry the primary one.
    """
    return union(
        select(Product.id.label("product_id"), Product.category_id.label("category_id")).where(
            Product.category_id.is_not(None)
        ),
        select(ProductCategory.product_id, ProductCategory.category_id),
    )


def products_in(category_ids):
    """Ids of the products listed under any of these categories (ids or a select of them)."""
    listed = memberships().subquery()
    return select(listed.c.product_id).where(listed.c.category_id.in_(category_ids))


def category_subtree_ids(category_id: int):
    """Ids of this category and all its descendants (recursive CTE)."""
    tree = select(Category.id).where(Category.id == category_id).cte("category_tree", recursive=True)
    tree = tree.union_all(select(Category.id).where(Category.parent_id == tree.c.id))
    return select(tree.c.id)


def ranked_order(rank):
    """A category page's "Önerilen", given a product's position there (a
    correlated scalar, NULL for one not ranked): the ranked first, by position;
    then the unranked; then those kept at the bottom, by position. Ties and the
    unranked go on by whatever the caller orders by next (by name)."""
    zone = case((rank.is_(None), 1), (rank >= CategoryProductOrder.BOTTOM, 2), else_=0)
    return (zone, rank.asc())
