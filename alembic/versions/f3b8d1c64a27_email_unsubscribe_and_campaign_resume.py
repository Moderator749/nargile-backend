"""One-click unsubscribe, and campaigns that resume after a restart.

``email_opt_outs``: the addresses that unsubscribed from campaign e-mail
(typed-in recipients have no consent column to clear).
``notification_campaigns.manual_recipients``: a typed-in list, kept so an
interrupted send can be worked out again; ``heartbeat_at``: touched as a
campaign sends, so one cut off by a restart can be told from one sending.

Revision ID: f3b8d1c64a27
Revises: e2a7c5d81b36
Create Date: 2026-11-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3b8d1c64a27"
down_revision: Union[str, None] = "e2a7c5d81b36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "email_opt_outs",
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("notification_campaigns", sa.Column("manual_recipients", postgresql.JSONB(), nullable=True))
    op.add_column("notification_campaigns", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_campaigns", "heartbeat_at")
    op.drop_column("notification_campaigns", "manual_recipients")
    op.drop_table("email_opt_outs")
