"""add industry board_name / industry_level (细分题材展示)

Revision ID: c4a2e7f0d9b1
Revises: a1c3e5f70921
Create Date: 2026-08-18 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4a2e7f0d9b1'
down_revision: Union[str, None] = 'a1c3e5f70921'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('industry_rankings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('board_name', sa.String(length=64), nullable=False, server_default='')
        )
        batch_op.add_column(
            sa.Column('industry_level', sa.String(length=16), nullable=False, server_default='')
        )


def downgrade() -> None:
    with op.batch_alter_table('industry_rankings', schema=None) as batch_op:
        batch_op.drop_column('industry_level')
        batch_op.drop_column('board_name')
