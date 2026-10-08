"""A corporate invoice address for individual accounts

An individual who asks to be invoiced in a firm's name ("Faturayı firmaya
istiyorum") now gives that firm's address too — the invoice has to be addressed
somewhere — and may have their orders delivered to it.

So ``customer_addresses`` gains a third kind, and the delivery preference a
fourth option for the one account that has two addresses to choose between. A
business account is unaffected: the firm is the account holder, so its billing
address is already its firm's.

Revision ID: e8b2f49d6c13
Revises: d1e7b53c89a4
Create Date: 2026-10-08
"""

from alembic import op

revision = "e8b2f49d6c13"
down_revision = "d1e7b53c89a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE addresskind ADD VALUE 'CORPORATE_BILLING'")
    op.execute("ALTER TYPE deliverymode ADD VALUE 'CORPORATE_BILLING_ADDRESS'")


def downgrade() -> None:
    # Postgres cannot drop a value from a type in use, so both are rebuilt
    # without the new ones; what referred to them has to go first. The firms'
    # addresses are deleted, and anyone having their orders sent to one falls
    # back to the option they would have had before it existed.
    op.execute("DELETE FROM customer_addresses WHERE kind = 'CORPORATE_BILLING'")
    op.execute(
        "UPDATE customer_profiles SET delivery_mode = 'BILLING_ADDRESS'"
        " WHERE delivery_mode = 'CORPORATE_BILLING_ADDRESS'"
    )

    op.execute("ALTER TYPE addresskind RENAME TO addresskind_old")
    op.execute("CREATE TYPE addresskind AS ENUM ('BILLING', 'DELIVERY')")
    op.execute(
        "ALTER TABLE customer_addresses ALTER COLUMN kind"
        " TYPE addresskind USING kind::text::addresskind"
    )
    op.execute("DROP TYPE addresskind_old")

    op.execute("ALTER TYPE deliverymode RENAME TO deliverymode_old")
    op.execute(
        "CREATE TYPE deliverymode AS ENUM ('BILLING_ADDRESS', 'CARGO_BRANCH', 'DIFFERENT_ADDRESS',"
        " 'OTHER_RECEIVER', 'CARGO_TO_BILLING', 'FREIGHT_TO_BILLING', 'INTERNATIONAL_CARGO',"
        " 'FREIGHT_FORWARDER')"
    )
    op.execute(
        "ALTER TABLE customer_profiles ALTER COLUMN delivery_mode"
        " TYPE deliverymode USING delivery_mode::text::deliverymode"
    )
    op.execute("DROP TYPE deliverymode_old")
