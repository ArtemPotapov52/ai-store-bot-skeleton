"""add saved procurement forecast plans"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6c1a8e7f2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "procurement_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("total_cost", sa.Numeric(12, 2), nullable=False),
        sa.Column("expected_revenue", sa.Numeric(12, 2), nullable=False),
        sa.Column("gross_profit", sa.Numeric(12, 2), nullable=False),
        sa.Column("gross_margin_percent", sa.Numeric(16, 2), nullable=False),
        sa.Column("return_on_cost_percent", sa.Numeric(16, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "CAST(total_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND total_cost > 0",
            name="ck_procurement_plans_total_cost",
        ),
        sa.CheckConstraint(
            "CAST(expected_revenue AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND expected_revenue > 0",
            name="ck_procurement_plans_expected_revenue",
        ),
        sa.CheckConstraint(
            "CAST(gross_profit AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_procurement_plans_gross_profit",
        ),
    )
    op.create_index("ix_procurement_plans_date_id", "procurement_plans", ["plan_date", "id"])

    op.create_table(
        "procurement_plan_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_id", sa.Integer(),
            sa.ForeignKey("procurement_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(),
            sa.ForeignKey("goods.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "category_id", sa.Integer(),
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category_name", sa.String(length=100), nullable=False),
        sa.Column("product_name", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(12, 2), nullable=False),
        sa.Column("sale_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("sale_price_mode", sa.String(length=12), nullable=False),
        sa.Column("total_cost", sa.Numeric(12, 2), nullable=False),
        sa.Column("expected_revenue", sa.Numeric(12, 2), nullable=False),
        sa.Column("gross_profit", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 100000",
            name="ck_procurement_plan_items_quantity",
        ),
        sa.CheckConstraint(
            "CAST(unit_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND unit_cost > 0",
            name="ck_procurement_plan_items_unit_cost",
        ),
        sa.CheckConstraint(
            "CAST(sale_price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND sale_price > 0",
            name="ck_procurement_plan_items_sale_price",
        ),
        sa.CheckConstraint(
            "sale_price_mode IN ('catalog', 'manual')",
            name="ck_procurement_plan_items_sale_mode",
        ),
        sa.CheckConstraint(
            "CAST(total_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND total_cost > 0",
            name="ck_procurement_plan_items_total_cost",
        ),
        sa.CheckConstraint(
            "CAST(expected_revenue AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND expected_revenue > 0",
            name="ck_procurement_plan_items_expected_revenue",
        ),
        sa.CheckConstraint(
            "CAST(gross_profit AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_procurement_plan_items_gross_profit",
        ),
        sa.UniqueConstraint("plan_id", "product_id", name="uq_procurement_plan_product"),
    )
    op.create_index(
        "ix_procurement_plan_items_plan_id_id",
        "procurement_plan_items",
        ["plan_id", "id"],
    )
    op.create_index(
        "ix_procurement_plan_items_product_id",
        "procurement_plan_items",
        ["product_id"],
    )
    op.create_index(
        "ix_procurement_plan_items_category_id",
        "procurement_plan_items",
        ["category_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_procurement_plan_items_product_id", table_name="procurement_plan_items")
    op.drop_index("ix_procurement_plan_items_category_id", table_name="procurement_plan_items")
    op.drop_index("ix_procurement_plan_items_plan_id_id", table_name="procurement_plan_items")
    op.drop_table("procurement_plan_items")
    op.drop_index("ix_procurement_plans_date_id", table_name="procurement_plans")
    op.drop_table("procurement_plans")
