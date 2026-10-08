"""International accounts and their addresses

A fourth kind of account, for customers outside Turkey, and the room their data
needs: a country and two address lines beside the Turkish mahalle/cadde fields,
a tax number long enough for a VAT number in any format, and phone columns long
enough for a dialling code.

``customer_addresses`` now holds two shapes in one table, told apart by
``country``. Existing rows are Turkish by definition, so the column defaults to
'TR' and ``district`` — required until now — only stays required for them, which
is what the new check constraint says.

Revision ID: b9e4c2a71f85
Revises: f3c6d81a4b27
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "b9e4c2a71f85"
down_revision = "f3c6d81a4b27"
branch_labels = None
depends_on = None

ADDRESS_SHAPE = (
    "CASE WHEN country = 'TR'"
    " THEN district IS NOT NULL AND (neighbourhood IS NOT NULL OR street IS NOT NULL)"
    " ELSE address_line1 IS NOT NULL END"
)


def upgrade() -> None:
    # ── enums ───────────────────────────────────────────────────────────────
    op.execute("ALTER TYPE accounttype ADD VALUE 'INTERNATIONAL'")
    op.execute("ALTER TYPE deliverymode ADD VALUE 'INTERNATIONAL_CARGO'")
    op.execute("ALTER TYPE deliverymode ADD VALUE 'FREIGHT_FORWARDER'")

    # ── room for numbers written another way ────────────────────────────────
    op.alter_column("users", "phone", type_=sa.String(length=30), existing_nullable=True)
    op.alter_column("users", "landline_phone", type_=sa.String(length=30), existing_nullable=True)
    op.alter_column(
        "customer_profiles", "tax_number", type_=sa.String(length=30), existing_nullable=True
    )

    # ── customer_addresses: the international shape ─────────────────────────
    op.add_column(
        "customer_addresses",
        sa.Column("country", sa.String(length=2), nullable=False, server_default="TR"),
    )
    op.add_column("customer_addresses", sa.Column("postal_code", sa.String(length=20), nullable=True))
    op.add_column("customer_addresses", sa.Column("address_line1", sa.String(length=200), nullable=True))
    op.add_column("customer_addresses", sa.Column("address_line2", sa.String(length=200), nullable=True))
    # A city or a state or a province, depending on the country.
    op.alter_column("customer_addresses", "city", type_=sa.String(length=80), existing_nullable=False)
    # İlçe is a Turkish field; the constraint keeps it required where it applies.
    op.alter_column("customer_addresses", "district", nullable=True, existing_type=sa.String(length=80))
    op.create_check_constraint("ck_customer_address_shape", "customer_addresses", ADDRESS_SHAPE)


def downgrade() -> None:
    op.drop_constraint("ck_customer_address_shape", "customer_addresses", type_="check")
    # International addresses have no district to go back to, and a Turkish
    # table has no room for them.
    op.execute("DELETE FROM customer_addresses WHERE country <> 'TR'")
    op.alter_column("customer_addresses", "district", nullable=False, existing_type=sa.String(length=80))
    op.alter_column("customer_addresses", "city", type_=sa.String(length=40), existing_nullable=False)
    op.drop_column("customer_addresses", "address_line2")
    op.drop_column("customer_addresses", "address_line1")
    op.drop_column("customer_addresses", "postal_code")
    op.drop_column("customer_addresses", "country")

    op.execute("UPDATE customer_profiles SET tax_number = left(tax_number, 11)")
    op.alter_column(
        "customer_profiles", "tax_number", type_=sa.String(length=11), existing_nullable=True
    )
    op.execute("UPDATE users SET phone = left(phone, 20), landline_phone = left(landline_phone, 20)")
    op.alter_column("users", "landline_phone", type_=sa.String(length=20), existing_nullable=True)
    op.alter_column("users", "phone", type_=sa.String(length=20), existing_nullable=True)

    # Accounts of the kind being removed go with it; so do their profiles and
    # addresses, by cascade.
    op.execute("DELETE FROM users WHERE account_type = 'INTERNATIONAL'")
    # Postgres cannot drop a value from an enum: both types are rebuilt without
    # the new ones, which is also why the rows above have to go first.
    op.execute("ALTER TYPE accounttype RENAME TO accounttype_old")
    op.execute("CREATE TYPE accounttype AS ENUM ('INDIVIDUAL', 'SOLE_PROPRIETOR', 'COMPANY')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN account_type DROP DEFAULT,"
        " ALTER COLUMN account_type TYPE accounttype USING account_type::text::accounttype,"
        " ALTER COLUMN account_type SET DEFAULT 'INDIVIDUAL'"
    )
    op.execute("DROP TYPE accounttype_old")

    op.execute("UPDATE customer_profiles SET delivery_mode = NULL WHERE delivery_mode IN ('INTERNATIONAL_CARGO', 'FREIGHT_FORWARDER')")
    op.execute("ALTER TYPE deliverymode RENAME TO deliverymode_old")
    op.execute(
        "CREATE TYPE deliverymode AS ENUM ('BILLING_ADDRESS', 'CARGO_BRANCH', 'DIFFERENT_ADDRESS',"
        " 'OTHER_RECEIVER', 'CARGO_TO_BILLING', 'FREIGHT_TO_BILLING')"
    )
    op.execute(
        "ALTER TABLE customer_profiles ALTER COLUMN delivery_mode"
        " TYPE deliverymode USING delivery_mode::text::deliverymode"
    )
    op.execute("DROP TYPE deliverymode_old")
