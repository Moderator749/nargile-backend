"""Users: ``is_superadmin``, the Sistem Yöneticisi above the roles.

Only it manages the panel team, and no one else may change, deactivate or
remove it (app.api.deps.guard_protected). The "Moderator" account — the
developer's, an admin — is made one here, when it exists;
scripts/set_superadmin.py does the same for any admin, on its own.

Revision ID: c3f9a1e7b204
Revises: b7e4c2d9a815
Create Date: 2026-11-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f9a1e7b204"
down_revision: Union[str, None] = "b7e4c2d9a815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_users_is_superadmin", "users", ["is_superadmin"])
    op.execute(
        "UPDATE users SET is_superadmin = true "
        "WHERE lower(username) = 'moderator' AND role IN ('SUPER_ADMIN', 'ADMIN') AND is_active"
    )


def downgrade() -> None:
    op.drop_index("ix_users_is_superadmin", table_name="users")
    op.drop_column("users", "is_superadmin")
