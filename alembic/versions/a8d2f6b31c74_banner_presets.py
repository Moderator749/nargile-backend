"""banner presets ("Hazır metinler"), each with its own date range

The special-day banner's ready-made texts move from the admin page's code into
a table the admin edits. A preset with "Otomatik yayınla" on is shown by the
banner by itself within its range. The texts the page offered are seeded, all
with automatic publishing off: the fixed national days carry their next date
(Europe/Istanbul, the whole day), the religious holidays — whose dates move —
none.

Revision ID: a8d2f6b31c74
Revises: f1c7a3e92d50
Create Date: 2026-10-14
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "a8d2f6b31c74"
down_revision = "f1c7a3e92d50"
branch_labels = None
depends_on = None

# (message, icon, start, end) — start/end in Turkey time (UTC+3).
SEED = [
    ("29 Ekim Cumhuriyet Bayramımız Kutlu Olsun!", "memorial", "2026-10-29 00:00+03", "2026-10-30 00:00+03"),
    ("10 Kasım: Ulu Önder Mustafa Kemal Atatürk'ü saygı, sevgi ve özlemle anıyoruz.", "memorial", "2026-11-10 00:00+03", "2026-11-11 00:00+03"),
    ("Mutlu Yıllar! Yeni yılda bereket ve bolluk dileriz.", "celebration", "2026-12-31 00:00+03", "2027-01-02 00:00+03"),
    ("23 Nisan Ulusal Egemenlik ve Çocuk Bayramı Kutlu Olsun!", "memorial", "2027-04-23 00:00+03", "2027-04-24 00:00+03"),
    ("19 Mayıs Atatürk'ü Anma, Gençlik ve Spor Bayramı Kutlu Olsun!", "memorial", "2027-05-19 00:00+03", "2027-05-20 00:00+03"),
    ("Ramazan Bayramınız Mübarek Olsun!", "celebration", None, None),
    ("Kurban Bayramınız Mübarek Olsun!", "celebration", None, None),
]


def upgrade() -> None:
    table = op.create_table(
        "banner_presets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message", sa.String(length=200), nullable=False),
        sa.Column("icon_type", sa.String(length=16), nullable=False, server_default="celebration"),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("is_scheduled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_banner_presets_schedule", "banner_presets", ["is_scheduled", "starts_at", "ends_at"])
    op.bulk_insert(
        table,
        [
            {
                "message": m,
                "icon_type": i,
                "starts_at": datetime.fromisoformat(s) if s else None,
                "ends_at": datetime.fromisoformat(e) if e else None,
                "is_scheduled": False,
                "sort_order": n,
            }
            for n, (m, i, s, e) in enumerate(SEED)
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_banner_presets_schedule", table_name="banner_presets")
    op.drop_table("banner_presets")
