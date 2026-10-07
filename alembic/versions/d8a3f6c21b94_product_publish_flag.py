"""products.is_published: keep a product off the storefront without deleting it

Defaults to true, so every product that exists today stays exactly as visible as
it is now; the admin switches new ones off per row.

Revision ID: d8a3f6c21b94
Revises: c7d2b4e85a31
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8a3f6c21b94'
down_revision: Union[str, Sequence[str], None] = 'c7d2b4e85a31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("products", "is_published")
