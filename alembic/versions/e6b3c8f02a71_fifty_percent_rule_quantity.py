"""the "%50 Kuralı" can count units (adet) instead of kilograms

Two new parameters: `coal_rule_basis` ("kg" or "quantity", which of the two
minimums the rule applies) and `coal_min_quantity` (the minimum in units).
Seeded so the rule keeps counting kilograms, as it always has; a row that
already exists is left as it is.

Revision ID: e6b3c8f02a71
Revises: d2a7e5c19b48
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "e6b3c8f02a71"
down_revision = "d2a7e5c19b48"
branch_labels = None
depends_on = None

ROWS = {"coal_rule_basis": "kg", "coal_min_quantity": "20"}


def upgrade() -> None:
    insert = sa.text(
        "INSERT INTO shop_settings (key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
    )
    for key, value in ROWS.items():
        op.execute(insert.bindparams(key=key, value=value))


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM shop_settings WHERE key IN :keys").bindparams(
            sa.bindparam("keys", tuple(ROWS), expanding=True)
        )
    )
