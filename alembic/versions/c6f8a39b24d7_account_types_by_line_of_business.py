"""Account types by line of business

The two business cards used to ask for a legal form — şahıs firması or şirket —
and now ask what the customer sells: Kafe & Restorant, or Nargile & Tobacco
Shop. The legal form moved into the invoice section of the form, where it was
already being stored (``customer_profiles.billing_entity_type``).

So this only adds the two new values. The old ones stay in the type: an account
created under them is a firm whose legal form is on its profile and whose line
of business was never asked, and inventing one for it would be worse than
leaving the row saying what it has always said. ``SELECTABLE_ACCOUNT_TYPES``
keeps them off the form.

Revision ID: c6f8a39b24d7
Revises: b9e4c2a71f85
Create Date: 2026-10-08
"""

from alembic import op

revision = "c6f8a39b24d7"
down_revision = "b9e4c2a71f85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE accounttype ADD VALUE 'CAFE'")
    op.execute("ALTER TYPE accounttype ADD VALUE 'TOBACCO_SHOP'")


def downgrade() -> None:
    # The accounts, and the two values, go together: Postgres cannot drop a
    # value from a type in use, so the type is rebuilt without them.
    op.execute("DELETE FROM users WHERE account_type IN ('CAFE', 'TOBACCO_SHOP')")
    op.execute("ALTER TYPE accounttype RENAME TO accounttype_old")
    op.execute(
        "CREATE TYPE accounttype AS ENUM"
        " ('INDIVIDUAL', 'SOLE_PROPRIETOR', 'COMPANY', 'INTERNATIONAL')"
    )
    op.execute(
        "ALTER TABLE users ALTER COLUMN account_type DROP DEFAULT,"
        " ALTER COLUMN account_type TYPE accounttype USING account_type::text::accounttype,"
        " ALTER COLUMN account_type SET DEFAULT 'INDIVIDUAL'"
    )
    op.execute("DROP TYPE accounttype_old")
