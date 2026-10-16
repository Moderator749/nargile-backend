from sqlalchemy import select, union

from app.models.catalog import Category, Product, ProductCategory

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
