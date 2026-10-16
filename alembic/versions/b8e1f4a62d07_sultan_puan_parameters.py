"""Sultan Puan: loyalty rules as store parameters, and the rename from ParaPuan

The loyalty rules move from constants in ``app.services.wallet`` into
``shop_settings`` ("Parametreler"), seeded with the rules the shop has run on:
10% on a single order of ₺20,000–29,999.99, 1 point = ₺1, and an annual bonus
of 1,000 points once paid orders under ₺20,000 reach ₺20,000 within 365 days
(which takes over from Kumbarapuan). A row that already exists is left as it is.

The program is renamed "Sultan Puan". The ledger keeps its source names
(PARAPUAN, KUMBARA) and its notes, so no record changes; what customers read
does: the Terms text and the homepage's loyalty card — the card only if it
still carries its original title, so an admin's own wording is not overwritten.

Revision ID: b8e1f4a62d07
Revises: a4d7e2c9b513
Create Date: 2026-10-13
"""

import json

import sqlalchemy as sa
from alembic import op

from app.services.store_parameters import DEFAULT_LOYALTY, loyalty_as_rows

revision = "b8e1f4a62d07"
down_revision = "a4d7e2c9b513"
branch_labels = None
depends_on = None

OLD_CARD = {
    "title": "ParaPuan & Kumbarapuanı",
    "highlight_label": "ParaPuan",
    "details": [
        "20.000 – 29.999,99 TL arası %10 ParaPuan (1 yıl geçerli)",
        "20.000 TL altı parça alımlarda toplam 20.000 TL'yi aşınca otomatik %5 Kumbarapuanı birikimi",
    ],
}
NEW_CARD = {
    "title": "Sultan Puan",
    "highlight_label": "Sultan Puan",
    "details": [
        "20.000 – 29.999,99 TL arası siparişlerde %10 Sultan Puan (1 yıl geçerli)",
        "20.000 TL altı siparişleriniz son 365 günde 20.000 TL'ye ulaşınca 1.000 Sultan Puan yıllık bonus",
    ],
}


def _card(old: dict, new: dict) -> None:
    op.execute(
        sa.text(
            "UPDATE advantages SET title = :title, highlight_label = :label, details = CAST(:details AS JSONB) "
            "WHERE title = :old_title"
        ).bindparams(
            title=new["title"], label=new["highlight_label"], details=json.dumps(new["details"], ensure_ascii=False),
            old_title=old["title"],
        )
    )


def upgrade() -> None:
    insert = sa.text(
        "INSERT INTO shop_settings (key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
    )
    for key, value in loyalty_as_rows(DEFAULT_LOYALTY).items():
        op.execute(insert.bindparams(key=key, value=value))

    op.execute("UPDATE legal_documents SET body = replace(body, 'ParaPuan', 'Sultan Puan') WHERE body LIKE '%ParaPuan%'")
    _card(OLD_CARD, NEW_CARD)


def downgrade() -> None:
    _card(NEW_CARD, OLD_CARD)
    op.execute("UPDATE legal_documents SET body = replace(body, 'Sultan Puan', 'ParaPuan') WHERE body LIKE '%Sultan Puan%'")
    op.execute(
        sa.text("DELETE FROM shop_settings WHERE key IN :keys").bindparams(
            sa.bindparam("keys", tuple(loyalty_as_rows(DEFAULT_LOYALTY)), expanding=True)
        )
    )
