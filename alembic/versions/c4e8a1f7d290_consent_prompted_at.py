"""When a customer was asked for campaign consent

The one-time "Kampanyalardan haberdar olun" card asks every account that has
never been asked — the customers the old store brought over — and is not
shown again once answered. A new sign-up is stamped as asked, since the form
asks.

Revision ID: c4e8a1f7d290
Revises: b9d4e7a2c815
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e8a1f7d290"
down_revision = "b9d4e7a2c815"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("consent_prompted_at", sa.DateTime(timezone=True), nullable=True))
    # Every existing account starts unasked: nothing tells an imported account
    # from one that signed up here reliably, and asking once more is harmless.

def downgrade() -> None:
    op.drop_column("users", "consent_prompted_at")
