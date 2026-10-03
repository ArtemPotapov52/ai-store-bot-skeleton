"""add product expense records"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, None] = "partner_api_v1_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_expenses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_name", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 100000",
            name="ck_product_expenses_quantity",
        ),
        sa.CheckConstraint(
            "CAST(total_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND total_cost > 0",
            name="ck_product_expenses_total_cost",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["goods.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_product_expenses_created_at_id",
        "product_expenses",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_product_expenses_product_created",
        "product_expenses",
        ["product_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_expenses_product_created", table_name="product_expenses")
    op.drop_index("ix_product_expenses_created_at_id", table_name="product_expenses")
    op.drop_table("product_expenses")
