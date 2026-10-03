"""Add partner API credentials and idempotency records.

Revision ID: partner_api_v1_001
Revises: c9d0e1f2a3b4
Create Date: 2026-09-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "partner_api_v1_001"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "partner_api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("key_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_partner_api_keys_revoked_at", "partner_api_keys", ["revoked_at"], unique=False,
    )
    op.create_index(
        "ix_partner_api_keys_user_revoked",
        "partner_api_keys",
        ["user_id", "revoked_at"],
        unique=False,
    )

    op.create_table(
        "api_idempotency",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "api_key_id",
            sa.Integer(),
            sa.ForeignKey("partner_api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("api_key_id", "idempotency_key", name="uq_api_idempotency_key"),
    )
    op.create_index(
        "ix_api_idempotency_created", "api_idempotency", ["created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_api_idempotency_created", table_name="api_idempotency")
    op.drop_table("api_idempotency")
    op.drop_index("ix_partner_api_keys_user_revoked", table_name="partner_api_keys")
    op.drop_index("ix_partner_api_keys_revoked_at", table_name="partner_api_keys")
    op.drop_table("partner_api_keys")
