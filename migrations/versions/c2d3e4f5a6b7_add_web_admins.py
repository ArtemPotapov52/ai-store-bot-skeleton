"""add web_admins (personal web panel logins bound to bot roles)

Revision ID: c2d3e4f5a6b7
Revises: a0b1c2d3e4f5
Create Date: 2026-09-18 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'a0b1c2d3e4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if 'web_admins' not in inspector.get_table_names():
        op.create_table(
            'web_admins',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('login', sa.String(length=64), nullable=False),
            sa.Column('password_hash', sa.String(length=255), nullable=False),
            sa.Column('role_id', sa.Integer(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['role_id'], ['roles.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('login'),
        )
        op.create_index('ix_web_admins_login', 'web_admins', ['login'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if 'web_admins' in inspector.get_table_names():
        try:
            op.drop_index('ix_web_admins_login', table_name='web_admins')
        except Exception:
            pass
        op.drop_table('web_admins')
