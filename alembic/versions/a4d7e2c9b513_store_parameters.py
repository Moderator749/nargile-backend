"""Store parameters: the checkout's commercial rules as panel settings

The free shipping threshold, the coal rule (minimum weight and the share of the
cart that makes it apply) and the havale/EFT and "ciro" discount ladders move
from constants in the checkout engine into ``shop_settings``, where the
"Parametreler" page edits them.

A ladder is stored as a JSON list, which can outgrow 200 characters, so
``shop_settings.value`` becomes TEXT. The rows are seeded with the rules the
shop has run on so far; a row that already exists is left as it is.

Revision ID: a4d7e2c9b513
Revises: e7c4b19a2f60
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

from app.services.store_parameters import DEFAULT_PARAMETERS, parameters_as_rows

revision = "a4d7e2c9b513"
down_revision = "e7c4b19a2f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("shop_settings", "value", type_=sa.Text(), existing_type=sa.String(length=200))
    insert = sa.text(
        "INSERT INTO shop_settings (key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
    )
    for key, value in parameters_as_rows(DEFAULT_PARAMETERS).items():
        op.execute(insert.bindparams(key=key, value=value))


def downgrade() -> None:
    # The engine falls back to its defaults once these rows are gone.
    op.execute(
        sa.text("DELETE FROM shop_settings WHERE key IN :keys").bindparams(
            sa.bindparam("keys", tuple(parameters_as_rows(DEFAULT_PARAMETERS)), expanding=True)
        )
    )
    op.alter_column("shop_settings", "value", type_=sa.String(length=200), existing_type=sa.Text())
