"""variant_prices: no price of 0 or below

Revision ID: a7c4e2b9d013
Revises: f2c8a4d6e917
Create Date: 2026-11-27 14:00:00.000000

A CHECK (price_excl_tax > 0 AND price_incl_tax > 0) behind the application
guard (pricing_engine.require_positive): an empty cell means "not sold to this
group", never "free". Added NOT VALID (a brief lock only, lock_timeout 5s) and
then validated, which scans the table without blocking reads or writes.
Production had 0 such rows on 2026-11-27.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a7c4e2b9d013"
down_revision: Union[str, None] = "f2c8a4d6e917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE variant_prices ADD CONSTRAINT ck_variant_prices_positive "
        "CHECK (price_excl_tax > 0 AND price_incl_tax > 0) NOT VALID"
    )
    op.execute("ALTER TABLE variant_prices VALIDATE CONSTRAINT ck_variant_prices_positive")


def downgrade() -> None:
    op.drop_constraint("ck_variant_prices_positive", "variant_prices", type_="check")
