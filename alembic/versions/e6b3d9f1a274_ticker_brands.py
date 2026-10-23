"""The brand ticker's names become brands

The homepage's brand ticker now shows the brands list ("Markalar"), with
their logos. The names it showed until now that the list did not have
(compared by name, slug or search term, ignoring case) are added to it,
active, at the end and in the ticker's order — so the ticker shows them all
still, and each gets its page.

Revision ID: e6b3d9f1a274
Revises: d5a2c8e4f193
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

from app.core.slugs import slugify

revision = "e6b3d9f1a274"
down_revision = "d5a2c8e4f193"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    names = conn.execute(
        sa.text("SELECT data->'brands'->'items' FROM site_content WHERE key = 'homepage'")
    ).scalar()
    if not isinstance(names, list):
        return
    known: set[str] = set()
    slugs: set[str] = set()
    last = 0
    for name, slug, term, order in conn.execute(sa.text("SELECT name, slug, search_term, sort_order FROM brands")):
        known |= {name.casefold(), slug.casefold(), term.casefold()}
        slugs.add(slug)
        last = max(last, order or 0)
    for raw in names:
        name = str(raw).strip()
        base = slugify(name)
        if not base or name.casefold() in known or base in known:
            continue
        slug, n = base, 2
        while slug in slugs:
            slug, n = f"{base}-{n}", n + 1
        last += 10
        conn.execute(
            sa.text(
                "INSERT INTO brands (name, slug, search_term, is_active, sort_order) VALUES (:name, :slug, :term, true, :order)"
            ),
            {"name": name[:80], "slug": slug, "term": name.lower()[:100], "order": last},
        )
        known |= {name.casefold(), base}
        slugs.add(slug)


def downgrade() -> None:
    # The brands it added may have logos and descriptions by now: kept.
    pass
