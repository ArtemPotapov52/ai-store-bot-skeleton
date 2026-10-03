"""add payments table with idempotency

Revision ID: 8xk049bo647i
Revises: 3f9d1b7a4a6c
Create Date: 2025-08-19 15:20:54.598425

"""
from typing import Union, Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "8xk049bo647i"
down_revision = "c720febc8cd2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'payments' in existing_tables:
        print("Table 'payments' already exists, skipping creation.")
        return

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, index=True),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),  # pending/succeeded/failed
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("provider", "external_id", name="uq_payment_provider_ext"),
    )


def downgrade():
    op.drop_table("payments")
