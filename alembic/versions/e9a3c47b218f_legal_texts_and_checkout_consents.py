"""Legal texts the shop can edit, and what each order agreed to

The three documents (gizlilik kuralları, mesafeli satış sözleşmesi, şartlar ve
kurallar) plus the short notice shown in the cart become rows an admin edits,
and are seeded with the texts they were first written with. Checkout reads them
live, so changing a text changes what the next buyer is shown and accepts.

Orders gain what the blueprint asks them to remember: whether the invoice goes
to a firm (and to which), whether the buyer asked for their card to be saved by
the payment provider, and when the two agreements were accepted.

Revision ID: e9a3c47b218f
Revises: d7f4b16e8c52
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

from app.core.legal_texts import DEFAULT_LEGAL

revision = "e9a3c47b218f"
down_revision = "d7f4b16e8c52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    legal = op.create_table(
        "legal_documents",
        sa.Column("key", sa.String(length=32), primary_key=True),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.bulk_insert(
        legal,
        [{"key": key, "title": title, "body": body} for key, (title, body) in DEFAULT_LEGAL.items()],
    )

    op.add_column(
        "orders",
        sa.Column("invoice_to_company", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("orders", sa.Column("invoice_company_title", sa.String(length=200), nullable=True))
    op.add_column("orders", sa.Column("invoice_tax_office", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("invoice_tax_number", sa.String(length=11), nullable=True))
    op.add_column("orders", sa.Column("save_card", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("agreements_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Lossy: the edited texts and the consent timestamps are dropped with the rows.
    op.drop_column("orders", "agreements_accepted_at")
    op.drop_column("orders", "save_card")
    op.drop_column("orders", "invoice_tax_number")
    op.drop_column("orders", "invoice_tax_office")
    op.drop_column("orders", "invoice_company_title")
    op.drop_column("orders", "invoice_to_company")
    op.drop_table("legal_documents")
