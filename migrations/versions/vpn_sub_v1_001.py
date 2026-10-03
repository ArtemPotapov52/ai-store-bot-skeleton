"""Add per-customer VPN subscription proxy links."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "vpn_sub_v1_001"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "goods",
        sa.Column("is_vpn_subscription", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_goods_is_vpn_subscription", "goods", ["is_vpn_subscription"], unique=False)
    op.create_table(
        "vpn_subscription_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_vpn_subscription_links_user_id", "vpn_subscription_links", ["user_id"])
    op.create_index(
        "ix_vpn_subscription_links_user_revoked",
        "vpn_subscription_links",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_vpn_subscription_links_user_revoked", table_name="vpn_subscription_links")
    op.drop_index("ix_vpn_subscription_links_user_id", table_name="vpn_subscription_links")
    op.drop_table("vpn_subscription_links")
    op.drop_index("ix_goods_is_vpn_subscription", table_name="goods")
    op.drop_column("goods", "is_vpn_subscription")
