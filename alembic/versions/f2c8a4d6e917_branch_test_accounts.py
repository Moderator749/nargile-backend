"""Branch / test accounts: out of the stock and the reports

Revision ID: f2c8a4d6e917
Revises: e3b9d5f1a276
Create Date: 2026-11-25 16:00:00.000000

- users.exclude_from_stock_and_reports: "Stok ve Raporlardan Hariç Tut
  (Şube / Test Hesabı)", set from the admin panel.
- orders.exclude_from_stock_and_reports: the account's flag when the order
  was placed, so a past order stays what it was.

Both are booleans with a constant default: a catalogue-only change in
PostgreSQL 11+ (no rewrite of either table); lock_timeout keeps a busy table
from queueing reads behind the ALTER.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8a4d6e917"
down_revision: Union[str, None] = "e3b9d5f1a276"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("exclude_from_stock_and_reports", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("exclude_from_stock_and_reports", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("orders", "exclude_from_stock_and_reports")
    op.drop_column("users", "exclude_from_stock_and_reports")
