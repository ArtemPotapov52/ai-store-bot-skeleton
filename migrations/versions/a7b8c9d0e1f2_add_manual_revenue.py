"""add operator-entered revenue records"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "manual_revenues" in inspector.get_table_names():
        return

    op.create_table(
        "manual_revenues",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_name", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint("quantity > 0 AND quantity <= 100000", name="ck_manual_revenue_quantity"),
        sa.CheckConstraint(
            "CAST(unit_price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND unit_price > 0",
            name="ck_manual_revenue_unit_price",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_revenues_category_name", "manual_revenues", ["category_name"])
    op.create_index("ix_manual_revenues_created_at", "manual_revenues", ["created_at"])
    op.create_index("ix_manual_revenues_created_at_id", "manual_revenues", ["created_at", "id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "manual_revenues" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_manual_revenues_created_at_id", table_name="manual_revenues")
    op.drop_index("ix_manual_revenues_created_at", table_name="manual_revenues")
    op.drop_index("ix_manual_revenues_category_name", table_name="manual_revenues")
    op.drop_table("manual_revenues")
