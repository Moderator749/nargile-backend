"""the coal cargo rule follows the product's "Sepet Kuralı" switch, not a category

Coal used to be whatever was listed under the Nargile Kömürleri category (or a
subcategory). The rule is per product now, switched on in the product form's
"Genel Kurallar", so the products the category made coal get the switch on and
carts price exactly as before. Switches already on stay on.

Revision ID: d2a7e5c19b48
Revises: c4f9a2d81e63
Create Date: 2026-10-14

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d2a7e5c19b48"
down_revision: Union[str, None] = "c4f9a2d81e63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id FROM categories WHERE slug = 'nargile-komurleri'
            UNION ALL
            SELECT c.id FROM categories c JOIN tree ON c.parent_id = tree.id
        )
        UPDATE products SET is_cart_rule_applied = true
        WHERE category_id IN (SELECT id FROM tree)
           OR id IN (SELECT product_id FROM product_categories WHERE category_id IN (SELECT id FROM tree))
        """
    )


def downgrade() -> None:
    # The switches stay as they are: the category rule needs nothing restored.
    pass
