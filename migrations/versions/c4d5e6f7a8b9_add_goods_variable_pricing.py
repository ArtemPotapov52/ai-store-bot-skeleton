"""Add configurable per-unit pricing quantity bounds to goods."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "vpn_sub_v1_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "goods",
        sa.Column(
            "is_variable_pricing", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("goods", sa.Column("min_quantity", sa.Integer(), nullable=True))
    op.add_column("goods", sa.Column("max_quantity", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_goods_variable_quantity_range",
        "goods",
        "(is_variable_pricing = false AND min_quantity IS NULL AND max_quantity IS NULL) "
        "OR (is_variable_pricing = true AND min_quantity >= 1 "
        "AND max_quantity >= min_quantity AND max_quantity <= 99)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_goods_variable_quantity_range", "goods", type_="check")
    op.drop_column("goods", "max_quantity")
    op.drop_column("goods", "min_quantity")
    op.drop_column("goods", "is_variable_pricing")
