"""communication: message log, password-reset tokens, campaigns, consent

- notification_logs: every e-mail and SMS sent, with its driver and outcome.
- password_reset_tokens: "Şifremi unuttum" secrets, stored as hashes only.
- notification_campaigns: the admin's bulk e-mail / SMS.
- users.marketing_email_consent_at / marketing_sms_consent_at: ticari
  elektronik ileti onayı per channel; null for everyone until they give it.

Revision ID: d7a4c2e86b19
Revises: c5e1d9a47f20
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d7a4c2e86b19"
down_revision = "c5e1d9a47f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("marketing_email_consent_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("marketing_sms_consent_at", sa.DateTime(timezone=True)))

    op.create_table(
        "notification_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("audience", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=200)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_format", sa.String(length=8), nullable=False, server_default="text"),
        sa.Column("sms_segments", sa.Integer()),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="queued"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("notification_campaigns.id", ondelete="SET NULL")),
        sa.Column("subject", sa.String(length=200)),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text()),
        sa.Column("provider_ref", sa.String(length=100)),
        sa.Column("meta", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_notification_logs_created", "notification_logs", ["created_at"])
    op.create_index("ix_notification_logs_campaign_id", "notification_logs", ["campaign_id"])
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_password_reset_tokens_user_created", "password_reset_tokens", ["user_id", "created_at"])
    op.create_index("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"])


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_user_created", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_notification_logs_campaign_id", table_name="notification_logs")
    op.drop_index("ix_notification_logs_created", table_name="notification_logs")
    op.drop_table("notification_logs")
    op.drop_table("notification_campaigns")
    op.drop_column("users", "marketing_sms_consent_at")
    op.drop_column("users", "marketing_email_consent_at")
