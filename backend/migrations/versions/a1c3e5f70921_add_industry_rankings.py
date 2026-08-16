"""add industry_rankings (行业榜)

Revision ID: a1c3e5f70921
Revises: 4b8cd2c5502d
Create Date: 2026-08-16 10:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c3e5f70921'
down_revision: Union[str, None] = '4b8cd2c5502d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('industry_rankings',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=False),
    sa.Column('industry', sa.String(length=64), nullable=False),
    sa.Column('industry_code', sa.String(length=16), nullable=False),
    sa.Column('heat_score', sa.Float(), nullable=False),
    sa.Column('news_count', sa.Integer(), nullable=False),
    sa.Column('fund_flow_net', sa.Float(), nullable=True),
    sa.Column('change_pct', sa.Float(), nullable=True),
    sa.Column('resonance', sa.String(length=16), nullable=False),
    sa.Column('rating', sa.String(length=2), nullable=False),
    sa.Column('leader_stocks_json', sa.Text(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['impact_snapshots.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('industry_rankings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_industry_rankings_snapshot_id'), ['snapshot_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('industry_rankings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_industry_rankings_snapshot_id'))

    op.drop_table('industry_rankings')
