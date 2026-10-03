"""add counted stock and shared delivery text to goods

Revision ID: a0b1c2d3e4f5
Revises: e9f0a1b2c3d4
Create Date: 2026-09-15 16:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("goods")}

    if "stock_quantity" not in columns:
        op.add_column(
            "goods",
            sa.Column("stock_quantity", sa.Integer(), nullable=False, server_default="0"),
        )
    if "delivery_text" not in columns:
        op.add_column("goods", sa.Column("delivery_text", sa.Text(), nullable=True))

    constraints = {constraint.get("name") for constraint in inspector.get_check_constraints("goods")}
    if "ck_goods_stock_quantity_nonnegative" not in constraints:
        op.create_check_constraint(
            "ck_goods_stock_quantity_nonnegative", "goods", "stock_quantity >= 0"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    constraints = {constraint.get("name") for constraint in inspector.get_check_constraints("goods")}
    if "ck_goods_stock_quantity_nonnegative" in constraints:
        op.drop_constraint("ck_goods_stock_quantity_nonnegative", "goods", type_="check")
    columns = {column["name"] for column in inspector.get_columns("goods")}
    if "delivery_text" in columns:
        op.drop_column("goods", "delivery_text")
    if "stock_quantity" in columns:
        op.drop_column("goods", "stock_quantity")
