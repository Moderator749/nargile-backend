"""What an international customer buys for

The international form asked whether the account was a business or a private
one; it now asks which kind of business — a wholesaler or a kafe — because
there is no private export customer and because it is the first thing the sales
team needs before quoting.

That answer used to be thrown away once it had decided which fields to require.
It is worth keeping, so it gets a column of its own: the counterpart of the two
domestic business cards, which are kept in ``users.account_type``.

Revision ID: d1e7b53c89a4
Revises: c6f8a39b24d7
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d1e7b53c89a4"
down_revision = "c6f8a39b24d7"
branch_labels = None
depends_on = None

INTERNATIONAL_KIND = postgresql.ENUM(
    "WHOLESALER", "CAFE", name="internationalkind", create_type=False
)


def upgrade() -> None:
    INTERNATIONAL_KIND.create(op.get_bind(), checkfirst=False)
    op.add_column(
        "customer_profiles",
        sa.Column("international_kind", INTERNATIONAL_KIND, nullable=True),
    )
    # Nothing to backfill: the accounts registered while the question was
    # "business or private" answered a question this column does not ask, and
    # the ones that said "business" never said which kind.


def downgrade() -> None:
    op.drop_column("customer_profiles", "international_kind")
    INTERNATIONAL_KIND.drop(op.get_bind(), checkfirst=False)
