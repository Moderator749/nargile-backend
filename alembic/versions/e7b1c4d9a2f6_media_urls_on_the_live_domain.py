"""Photos stored under the staging address point at the live domain

Before go-live the API wrote its uploads' addresses with the staging host
(PUBLIC_MEDIA_URL = https://nargilemalzemesi-staging.vercel.app/api/backend),
and every product photo, slide and homepage box saved then still carries it:
275 published products, the homepage document and its slides (2026-10-28).
They load — the staging host still proxies /api/backend — but a search
engine reads the shop's photos under another site's name, and the day that
alias goes they break. Each is rewritten onto https://www.nargilemalzemesi.com,
the same file at the address the API writes today.

Only the shop's content: the catalog, the homepage and its boxes, the
announcements and the legal texts — every text and JSON column of those
tables. Orders, payments and notification logs are records of what was, and
keep their addresses (which the staging alias still answers).

Downgrade does nothing: an address on the live domain cannot be told apart
from one that was always there, and the old one is not wanted back.

Revision ID: e7b1c4d9a2f6
Revises: d6a3f9b2e417
Create Date: 2026-10-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b1c4d9a2f6"
down_revision: Union[str, None] = "d6a3f9b2e417"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "https://nargilemalzemesi-staging.vercel.app/api/backend/media/"
NEW = "https://www.nargilemalzemesi.com/api/backend/media/"

TABLES = (
    "products",
    "product_variants",
    "categories",
    "brands",
    "hero_slides",
    "promo_boxes",
    "site_content",
    "site_announcements",
    "popup_announcements",
    "banner_presets",
    "legal_documents",
)


def upgrade() -> None:
    conn = op.get_bind()
    columns = conn.execute(
        sa.text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY(:tables) "
            "AND data_type IN ('text', 'character varying', 'jsonb')"
        ),
        {"tables": list(TABLES)},
    ).all()
    for table, column, data_type in columns:
        # JSON keeps a URL's slashes as they are, so its text is replaced like any other.
        value = f'"{column}"::text' if data_type == "jsonb" else f'"{column}"'
        replaced = f"replace({value}, :old, :new)"
        conn.execute(
            sa.text(
                f'UPDATE "{table}" SET "{column}" = '
                + (f"CAST({replaced} AS jsonb)" if data_type == "jsonb" else replaced)
                + f" WHERE strpos({value}, :old) > 0"
            ),
            {"old": OLD, "new": NEW},
        )


def downgrade() -> None:
    pass
