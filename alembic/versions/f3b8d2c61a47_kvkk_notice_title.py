"""The privacy text is titled as the footer links to it: "KVKK Aydınlatma Metni"

The footer's link and the page's address (/sozlesmeler/kvkk-aydinlatma-metni)
name it "KVKK Aydınlatma Metni"; its seeded title was "Gizlilik Kuralları".
Only a title still as seeded is changed: one the admin rewrote stays theirs.

Revision ID: f3b8d2c61a47
Revises: c7e2a9d4f013
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b8d2c61a47"
down_revision = "c7e2a9d4f013"
branch_labels = None
depends_on = None

OLD, NEW = "Gizlilik Kuralları", "KVKK Aydınlatma Metni"


def _retitle(old: str, new: str) -> None:
    op.get_bind().execute(
        sa.text("UPDATE legal_documents SET title = :new WHERE key = 'PRIVACY' AND title = :old"),
        {"old": old, "new": new},
    )


def upgrade() -> None:
    _retitle(OLD, NEW)


def downgrade() -> None:
    _retitle(NEW, OLD)
