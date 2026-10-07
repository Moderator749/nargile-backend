"""banner icon type

Revision ID: a7c3e9d41b20
Revises: 29f1634dcf11
Create Date: 2026-10-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d41b20'
down_revision: Union[str, Sequence[str], None] = '29f1634dcf11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('site_announcements', sa.Column('icon_type', sa.String(length=16), nullable=False, server_default='celebration'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('site_announcements', 'icon_type')
