"""Saved delivery addresses ("Adreslerim")

A customer keeps an address book in "Hesabım" and picks from it at checkout,
so ``customer_addresses`` gains a SHIPPING kind with a label of its own ("Ev",
"İş"). An account still has at most one address of each invoice kind; the
uniqueness becomes a partial index over those two kinds, so saved delivery
addresses can be many. An order keeps its own frozen copy, as before.

Revision ID: b9d4e7a2c815
Revises: f3b8d2c61a47
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "b9d4e7a2c815"
down_revision = "f3b8d2c61a47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE addresskind ADD VALUE IF NOT EXISTS 'SHIPPING'")
    op.add_column("customer_addresses", sa.Column("label", sa.String(length=60), nullable=True))
    op.drop_constraint("uq_customer_address_kind", "customer_addresses", type_="unique")
    # Named by the old values only: a value added to an enum cannot be used in
    # the transaction that added it.
    op.create_index(
        "uq_customer_address_kind",
        "customer_addresses",
        ["user_id", "kind"],
        unique=True,
        postgresql_where=sa.text("kind IN ('BILLING', 'CORPORATE_BILLING')"),
    )


def downgrade() -> None:
    # The saved delivery addresses go; the invoice addresses are untouched.
    op.execute("DELETE FROM customer_addresses WHERE kind = 'SHIPPING'")
    op.drop_index("uq_customer_address_kind", table_name="customer_addresses")
    op.create_unique_constraint("uq_customer_address_kind", "customer_addresses", ["user_id", "kind"])
    op.drop_column("customer_addresses", "label")
    op.execute("ALTER TYPE addresskind RENAME TO addresskind_old")
    op.execute("CREATE TYPE addresskind AS ENUM ('BILLING', 'CORPORATE_BILLING')")
    op.execute(
        "ALTER TABLE customer_addresses ALTER COLUMN kind"
        " TYPE addresskind USING kind::text::addresskind"
    )
    op.execute("DROP TYPE addresskind_old")
