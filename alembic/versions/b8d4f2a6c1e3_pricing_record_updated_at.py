"""Pricing records: when each was last saved (``updated_at``).

"Cetvele yükle" then "Kaydet", or "Üzerine Yaz" on a code the destination
already holds, now overwrite a row instead of adding another; the row's
``updated_at`` says when. The lists go by it. Existing rows start at their
``created_at``.

Revision ID: b8d4f2a6c1e3
Revises: c5e1f7a3b902
Create Date: 2026-11-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f2a6c1e3"
down_revision: Union[str, None] = "c5e1f7a3b902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pricing_records", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE pricing_records SET updated_at = created_at")
    op.alter_column("pricing_records", "updated_at", nullable=False, server_default=sa.func.now())
    op.create_index("ix_pricing_records_updated_at", "pricing_records", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_pricing_records_updated_at", table_name="pricing_records")
    op.drop_column("pricing_records", "updated_at")
