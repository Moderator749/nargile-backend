"""Account types, customer profiles and structured addresses

Sign-up now asks who the customer is — bireysel, şahıs firması or şirket — and
stores the answer: a tax identity, a delivery preference, and addresses field by
field rather than as one line of free text.

``company_profiles`` is folded into the new ``customer_profiles``. It held the
company an admin recorded when approving a B2B application, which is the same
thing the new table holds for every account, so keeping both would mean two
places to look for one invoice address. Its ``billing_address`` / ``delivery_address``
JSONB columns are dropped without being converted: no code ever wrote them, so
there is nothing in them to convert. The company title and tax fields are
copied across.

Revision ID: f3c6d81a4b27
Revises: e4b7a9c15d38
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3c6d81a4b27"
down_revision = "e4b7a9c15d38"
branch_labels = None
depends_on = None

# Created once, explicitly, at the top of the upgrade: ``add_column`` would not
# create them at all and ``create_table`` would try to create them a second
# time, so ``create_type=False`` keeps both out of the business.
ACCOUNT_TYPE = postgresql.ENUM(
    "INDIVIDUAL", "SOLE_PROPRIETOR", "COMPANY", name="accounttype", create_type=False
)
BILLING_ENTITY = postgresql.ENUM(
    "SOLE_PROPRIETOR", "COMPANY", name="billingentity", create_type=False
)
DELIVERY_MODE = postgresql.ENUM(
    "BILLING_ADDRESS",
    "CARGO_BRANCH",
    "DIFFERENT_ADDRESS",
    "OTHER_RECEIVER",
    "CARGO_TO_BILLING",
    "FREIGHT_TO_BILLING",
    name="deliverymode",
    create_type=False,
)
ADDRESS_KIND = postgresql.ENUM("BILLING", "DELIVERY", name="addresskind", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (ACCOUNT_TYPE, BILLING_ENTITY, DELIVERY_MODE, ADDRESS_KIND):
        enum.create(bind, checkfirst=False)

    # ── users ───────────────────────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column("account_type", ACCOUNT_TYPE, nullable=False, server_default="INDIVIDUAL"),
    )
    op.add_column("users", sa.Column("username", sa.String(length=30), nullable=True))
    op.add_column("users", sa.Column("landline_phone", sa.String(length=20), nullable=True))
    # Case-insensitive and partial by nature: NULLs do not collide, so the many
    # accounts without a username are unaffected.
    op.execute("CREATE UNIQUE INDEX uq_users_username_lower ON users (lower(username))")

    # ── customer_profiles ───────────────────────────────────────────────────
    op.create_table(
        "customer_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("national_id", sa.String(length=11), nullable=True),
        sa.Column("billing_entity_type", BILLING_ENTITY, nullable=True),
        sa.Column("company_title", sa.String(length=255), nullable=True),
        sa.Column("tax_plate_name", sa.String(length=150), nullable=True),
        sa.Column("tax_office", sa.String(length=100), nullable=True),
        sa.Column("tax_number", sa.String(length=11), nullable=True),
        sa.Column("contact_name", sa.String(length=150), nullable=True),
        sa.Column("delivery_mode", DELIVERY_MODE, nullable=True),
        sa.Column("receiver_name", sa.String(length=150), nullable=True),
        sa.Column("receiver_phone", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    # ── customer_addresses ──────────────────────────────────────────────────
    op.create_table(
        "customer_addresses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", ADDRESS_KIND, nullable=False),
        sa.Column("neighbourhood", sa.String(length=120), nullable=True),
        sa.Column("street", sa.String(length=120), nullable=True),
        sa.Column("building_no", sa.String(length=20), nullable=True),
        sa.Column("building_name", sa.String(length=120), nullable=True),
        sa.Column("door_no", sa.String(length=20), nullable=True),
        sa.Column("district", sa.String(length=80), nullable=False),
        sa.Column("city", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", name="uq_customer_address_kind"),
    )
    op.create_index("ix_customer_addresses_user_id", "customer_addresses", ["user_id"])

    # ── company_profiles → customer_profiles ────────────────────────────────
    op.execute(
        """
        INSERT INTO customer_profiles (user_id, company_title, tax_number, tax_office)
        SELECT user_id, company_title, left(tax_number, 11), tax_office
        FROM company_profiles
        """
    )
    op.drop_table("company_profiles")


def downgrade() -> None:
    op.create_table(
        "company_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("company_title", sa.String(length=255), nullable=False),
        sa.Column("tax_number", sa.String(length=20), nullable=True),
        sa.Column("tax_office", sa.String(length=100), nullable=True),
        sa.Column("billing_address", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("delivery_address", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    # company_title was NOT NULL there, so only the profiles that have one can
    # go back; the rest were never company profiles to begin with.
    op.execute(
        """
        INSERT INTO company_profiles (user_id, company_title, tax_number, tax_office)
        SELECT user_id, company_title, tax_number, tax_office
        FROM customer_profiles
        WHERE company_title IS NOT NULL
        """
    )

    op.drop_index("ix_customer_addresses_user_id", table_name="customer_addresses")
    op.drop_table("customer_addresses")
    op.drop_table("customer_profiles")
    op.execute("DROP INDEX IF EXISTS uq_users_username_lower")
    op.drop_column("users", "landline_phone")
    op.drop_column("users", "username")
    op.drop_column("users", "account_type")

    bind = op.get_bind()
    for enum in (ADDRESS_KIND, DELIVERY_MODE, BILLING_ENTITY, ACCOUNT_TYPE):
        enum.drop(bind, checkfirst=False)
