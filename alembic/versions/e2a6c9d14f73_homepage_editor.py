"""Homepage editor: site_content document and hero slide layout

Revision ID: e2a6c9d14f73
Revises: d8c1f5a60b72
Create Date: 2026-10-12 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2a6c9d14f73"
down_revision: Union[str, None] = "d8c1f5a60b72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_content",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.add_column("hero_slides", sa.Column("text_align", sa.String(length=10), nullable=False, server_default="center"))
    op.add_column("hero_slides", sa.Column("image_focus_x", sa.Integer(), nullable=False, server_default="50"))
    op.add_column("hero_slides", sa.Column("image_focus_y", sa.Integer(), nullable=False, server_default="50"))
    op.add_column("hero_slides", sa.Column("image_zoom", sa.Integer(), nullable=False, server_default="100"))


def downgrade() -> None:
    op.drop_column("hero_slides", "image_zoom")
    op.drop_column("hero_slides", "image_focus_y")
    op.drop_column("hero_slides", "image_focus_x")
    op.drop_column("hero_slides", "text_align")
    op.drop_table("site_content")
