"""storefront ordering, visibility, images and user locale

Revision ID: e9f0a1b2c3d4
Revises: d7e8f9a0b1c2
Create Date: 2026-09-13 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("locale", sa.String(length=5), nullable=False, server_default="ru"))

    op.add_column("categories", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("categories", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("categories", sa.Column("image_ref", sa.Text(), nullable=True))
    op.create_index("ix_categories_sort_order", "categories", ["sort_order"])
    op.create_index("ix_categories_is_active", "categories", ["is_active"])

    op.add_column("goods", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("goods", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("goods", sa.Column("image_ref", sa.Text(), nullable=True))
    op.add_column("goods", sa.Column("availability_note", sa.String(length=64), nullable=True))
    op.create_index("ix_goods_sort_order", "goods", ["sort_order"])
    op.create_index("ix_goods_is_active", "goods", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_goods_is_active", table_name="goods")
    op.drop_index("ix_goods_sort_order", table_name="goods")
    op.drop_column("goods", "availability_note")
    op.drop_column("goods", "image_ref")
    op.drop_column("goods", "is_active")
    op.drop_column("goods", "sort_order")

    op.drop_index("ix_categories_is_active", table_name="categories")
    op.drop_index("ix_categories_sort_order", table_name="categories")
    op.drop_column("categories", "image_ref")
    op.drop_column("categories", "is_active")
    op.drop_column("categories", "sort_order")

    op.drop_column("users", "locale")
