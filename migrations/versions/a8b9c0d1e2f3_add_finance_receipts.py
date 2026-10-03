"""add finance cash receipts and optional expense source"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "finance_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint("source IN ('crypto', 'card', 'cash', 'other')", name="ck_finance_receipt_source"),
        sa.CheckConstraint("currency IN ('RUB', 'USDT', 'USD', 'BYN')", name="ck_finance_receipt_currency"),
        sa.CheckConstraint(
            "CAST(amount AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND amount > 0",
            name="ck_finance_receipt_amount_positive",
        ),
        sa.CheckConstraint(
            "CAST(amount_rub AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND amount_rub > 0",
            name="ck_finance_receipt_rub_positive",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("source", "reference", name="uq_finance_receipt_source_reference"),
    )
    op.create_index("ix_finance_receipts_source", "finance_receipts", ["source"])
    op.create_index("ix_finance_receipts_user_id", "finance_receipts", ["user_id"])
    op.create_index("ix_finance_receipts_received_at", "finance_receipts", ["received_at"])
    op.create_index("ix_finance_receipts_received_at_id", "finance_receipts", ["received_at", "id"])
    op.add_column("product_expenses", sa.Column("finance_receipt_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_product_expenses_finance_receipt_id_finance_receipts",
        "product_expenses", "finance_receipts", ["finance_receipt_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_product_expenses_finance_receipt_id", "product_expenses", ["finance_receipt_id"])


def downgrade() -> None:
    op.drop_index("ix_product_expenses_finance_receipt_id", table_name="product_expenses")
    op.drop_constraint("fk_product_expenses_finance_receipt_id_finance_receipts", "product_expenses", type_="foreignkey")
    op.drop_column("product_expenses", "finance_receipt_id")
    op.drop_index("ix_finance_receipts_received_at_id", table_name="finance_receipts")
    op.drop_index("ix_finance_receipts_received_at", table_name="finance_receipts")
    op.drop_index("ix_finance_receipts_user_id", table_name="finance_receipts")
    op.drop_index("ix_finance_receipts_source", table_name="finance_receipts")
    op.drop_table("finance_receipts")
