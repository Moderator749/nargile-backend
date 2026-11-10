"""A countdown on a banner preset ("Geri sayım").

``banner_presets.show_countdown``: on, the banner counts down to the preset's
end while it is live, as the default banner does to its expiry. Off on every
preset but the shop's sales days already seeded (Sevgililer Günü, Babalar
Günü): the column is born here, so this is the one moment their "never set"
can be told from an admin's "off".

Revision ID: e2a7c5d81b36
Revises: c7d2e5a9f041
Create Date: 2026-11-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a7c5d81b36"
down_revision: Union[str, None] = "c7d2e5a9f041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COUNTING_DOWN = ("Sevgililer Günü", "Babalar Günü")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "banner_presets",
        sa.Column("show_countdown", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    presets = sa.table("banner_presets", sa.column("title", sa.String), sa.column("show_countdown", sa.Boolean))
    op.execute(presets.update().where(presets.c.title.in_(COUNTING_DOWN)).values(show_countdown=True))


def downgrade() -> None:
    op.drop_column("banner_presets", "show_countdown")
