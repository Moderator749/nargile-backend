"""Admin notes: a lock (``is_locked``, ``locked_by_id``).

A locked note can't be deleted, alone or in a bulk delete, until it is
unlocked; only the admin who locked it and the "Moderator" account can unlock
it. Every existing note starts unlocked.

Revision ID: f4b8d2c6a913
Revises: e7c2a9f41b58
Create Date: 2026-11-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b8d2c6a913"
down_revision: Union[str, None] = "e7c2a9f41b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("admin_notes", sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(
        "admin_notes",
        sa.Column(
            "locked_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", name="fk_admin_notes_locked_by_id_users", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_admin_notes_locked_by_id_users", "admin_notes", type_="foreignkey")
    op.drop_column("admin_notes", "locked_by_id")
    op.drop_column("admin_notes", "is_locked")
