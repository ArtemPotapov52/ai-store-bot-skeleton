"""Track bot starters and privacy-minimal daily activity counters.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backfill current accounts as starters; new accounts default to not-started
    # until their first accepted /start is recorded.
    op.add_column(
        "users",
        sa.Column("has_started", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column(
        "users", "has_started", existing_type=sa.Boolean(), server_default=sa.false()
    )
    op.create_index("ix_users_has_started", "users", ["has_started"])
    op.create_table(
        "bot_user_daily_activity",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=False),
        sa.Column("start_click_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("interaction_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("start_click_count >= 0", name="ck_bot_activity_start_nonnegative"),
        sa.CheckConstraint("interaction_count >= 0", name="ck_bot_activity_interaction_nonnegative"),
        sa.ForeignKeyConstraint(
            ["telegram_id"], ["users.telegram_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("telegram_id", "activity_date"),
    )
    op.create_index(
        "ix_bot_activity_date", "bot_user_daily_activity", ["activity_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_bot_activity_date", table_name="bot_user_daily_activity")
    op.drop_table("bot_user_daily_activity")
    op.drop_index("ix_users_has_started", table_name="users")
    op.drop_column("users", "has_started")
