"""Rich text: descriptions, legal texts and notes stored as the editor's HTML

The four fields were plain text (paragraphs separated by blank lines, line
breaks inside them, "## " headings in the legal texts). The panel now edits
them with a rich-text editor and the storefront shows them as HTML, so every
stored value is converted once, with the same rules the API applies to new
input (app.core.rich_text): plain text becomes escaped paragraphs, anything
already HTML is cleaned. No column changes: all four are TEXT already.

Downgrade leaves the HTML in place: the plain form cannot be told apart from
text a person typed, and the storefront before this revision would show the
tags literally rather than lose any text.

Revision ID: d2a8e5b47c19
Revises: c7f1a9d24e86
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

from app.core.rich_text import to_rich_html

revision = "d2a8e5b47c19"
down_revision = "c7f1a9d24e86"
branch_labels = None
depends_on = None

# (table, key column, text column, empty value): products/categories store
# NULL for "none"; the two NOT NULL bodies store "".
FIELDS = (
    ("products", "id", "description", None),
    ("categories", "id", "description", None),
    ("legal_documents", "key", "body", ""),
    ("admin_notes", "id", "body", ""),
)


def upgrade() -> None:
    conn = op.get_bind()
    for table, key, column, empty in FIELDS:
        rows = conn.execute(sa.text(f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL")).all()
        for row_key, value in rows:
            converted = to_rich_html(value)
            converted = empty if converted is None else converted
            if converted != value:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {column} = :value WHERE {key} = :key"),
                    {"value": converted, "key": row_key},
                )


def downgrade() -> None:
    pass
