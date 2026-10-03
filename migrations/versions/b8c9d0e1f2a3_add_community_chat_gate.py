"""Add grandfathered optional and new-user mandatory community chat flags.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing accounts get False from this temporary default, then future
    # inserts inherit True after the database default is changed.
    op.add_column(
        "users",
        sa.Column(
            "community_chat_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "users",
        "community_chat_required",
        existing_type=sa.Boolean(),
        server_default=sa.true(),
    )
    op.add_column(
        "users",
        sa.Column(
            "community_prompt_seen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "community_prompt_seen")
    op.drop_column("users", "community_chat_required")
