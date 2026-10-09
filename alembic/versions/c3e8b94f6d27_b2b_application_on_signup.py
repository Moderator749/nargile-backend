"""A business sign-up opens its own application

Registering as a kafe, a tobacco shop or an export customer now creates a
pending B2B application in the same transaction, so a firm cannot sit at retail
prices with nobody aware of it.

That means an application can now come from abroad, where a VAT number is not
always issued and is not always short — so ``tax_number`` becomes optional and
gets the same room the profile's copy has.

Revision ID: c3e8b94f6d27
Revises: b5d2f18c4a93
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "c3e8b94f6d27"
down_revision = "b5d2f18c4a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "b2b_applications",
        "tax_number",
        type_=sa.String(length=30),
        existing_type=sa.String(length=20),
        nullable=True,
        existing_nullable=False,
    )


def downgrade() -> None:
    # The column was NOT NULL, so the applications that have no number — the
    # ones from abroad — cannot go back. They are the whole reason it changed.
    op.execute("DELETE FROM b2b_applications WHERE tax_number IS NULL")
    op.execute("UPDATE b2b_applications SET tax_number = left(tax_number, 20)")
    op.alter_column(
        "b2b_applications",
        "tax_number",
        type_=sa.String(length=20),
        existing_type=sa.String(length=30),
        nullable=False,
        existing_nullable=True,
    )
