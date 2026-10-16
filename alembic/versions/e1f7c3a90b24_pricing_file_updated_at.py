"""Pricing files: when a row was last saved into them

Shown beside the file's name in "Dosyalar", and the order the list is in.
Existing files start from their creation time.

Revision ID: e1f7c3a90b24
Revises: d6b2e8f41a37
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "e1f7c3a90b24"
down_revision = "d6b2e8f41a37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pricing_files",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # A file's last save is at least its newest row.
    op.execute(
        "UPDATE pricing_files f SET updated_at = COALESCE("
        "(SELECT max(r.created_at) FROM pricing_records r WHERE r.file_id = f.id), f.created_at)"
    )


def downgrade() -> None:
    op.drop_column("pricing_files", "updated_at")
