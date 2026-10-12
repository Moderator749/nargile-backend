from sqlalchemy import select

from app.models.catalog import Category

# Products in this category (or its subcategories) are coal for the cargo rules.
# The admin API will not rename this slug or delete the category: the rules
# would silently stop applying.
COAL_CATEGORY_SLUG = "nargile-komurleri"


def category_subtree(slug: str):
    """Ids of the category with this slug and all its descendants (recursive CTE)."""
    tree = select(Category.id).where(Category.slug == slug).cte("category_tree", recursive=True)
    tree = tree.union_all(select(Category.id).where(Category.parent_id == tree.c.id))
    return select(tree.c.id)
