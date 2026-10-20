"""Add traffic campaigns and clicks tables for ad procurement tracking.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-19 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "traffic_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.Column("cost", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_traffic_campaigns_tag", "traffic_campaigns", ["tag"], unique=True)

    op.create_table(
        "traffic_clicks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("traffic_campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("is_new_user", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_traffic_clicks_campaign_id", "traffic_clicks", ["campaign_id"])
    op.create_index("ix_traffic_clicks_created_at", "traffic_clicks", ["created_at"])
    op.create_index("ix_traffic_clicks_user_id", "traffic_clicks", ["user_id"])

    op.add_column(
        "users",
        sa.Column(
            "source_campaign_id",
            sa.Integer(),
            sa.ForeignKey("traffic_campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_users_source_campaign_id", "users", ["source_campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_users_source_campaign_id", table_name="users")
    op.drop_column("users", "source_campaign_id")
    op.drop_index("ix_traffic_clicks_user_id", table_name="traffic_clicks")
    op.drop_index("ix_traffic_clicks_created_at", table_name="traffic_clicks")
    op.drop_index("ix_traffic_clicks_campaign_id", table_name="traffic_clicks")
    op.drop_table("traffic_clicks")
    op.drop_index("ix_traffic_campaigns_tag", table_name="traffic_campaigns")
    op.drop_table("traffic_campaigns")
