"""Guest checkout: a buyer without an account

"Üye olmadan devam et" places an order under a guest row of its own
(User.is_guest): no usable password, left out of every lookup by e-mail or
phone, and any number of them may share an e-mail address — each guest
checkout is its own buyer, so nobody who knows an address can open another
guest's orders. The address stays unique among accounts.

Revision ID: f2a8c5e1b934
Revises: e7b1c4d9a2f6
Create Date: 2026-10-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a8c5e1b934"
down_revision: Union[str, None] = "e7b1c4d9a2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True, postgresql_where=sa.text("NOT is_guest"))


def downgrade() -> None:
    # Guest rows cannot outlive the column that tells them apart; their
    # orders would lose their buyer, so the downgrade refuses while any exist.
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM users WHERE is_guest LIMIT 1")).scalar():
        raise RuntimeError("Guest checkouts exist: downgrade would orphan their orders. Restore a backup instead.")
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.drop_column("users", "is_guest")
