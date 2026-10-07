"""coal dominant shipping footnote

Revision ID: 5ddac810bf81
Revises: d537ec2776ac
Create Date: 2026-10-06 15:43:19.910742

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5ddac810bf81'
down_revision: Union[str, Sequence[str], None] = 'd537ec2776ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD = "Kömür, sepet tutarının %50'sini aşarsa veya 20 kg'ın altındaysa kargo ücretlidir."
NEW = (
    "Kömür ağırlıklı siparişlerde (sepetin %50'sinden fazlası kömürse) ücretsiz kargo için "
    "en az 20 kg kömür gereklidir."
)


def upgrade() -> None:
    """The 20 kg coal minimum now applies only to coal-dominant carts. Reword the
    seeded footnote, unless an admin has already edited it."""
    op.execute(sa.text("UPDATE advantages SET footnote = :new WHERE footnote = :old").bindparams(new=NEW, old=OLD))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("UPDATE advantages SET footnote = :old WHERE footnote = :new").bindparams(new=NEW, old=OLD))
