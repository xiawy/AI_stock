"""add analysis task state columns (A5: task state in DB)

Revision ID: d5b9f31c7a02
Revises: c4a2e7f0d9b1
Create Date: 2026-10-17 20:20:00.000000

A5 把分析任务的状态从"进程内存"搬到表里，让任意 worker 都能服务任意任务。
`init_db()` 里另有一段幂等补列（同一批列），因此老库即使不跑 alembic 也能启动；
此处保留正式迁移是为了 alembic 版本链完整、autogenerate 不再产生噪声 diff。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5b9f31c7a02'
down_revision: Union[str, None] = 'c4a2e7f0d9b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('analysis_tasks', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('progress_json', sa.Text(), nullable=False, server_default='{}')
        )
        batch_op.add_column(
            sa.Column('result_json', sa.Text(), nullable=False, server_default='')
        )
        batch_op.add_column(
            sa.Column('owner', sa.String(length=96), nullable=False, server_default='')
        )
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column('control', sa.String(length=16), nullable=False, server_default='')
        )
        batch_op.add_column(sa.Column('control_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index('ix_analysis_tasks_owner', ['owner'])
        batch_op.create_index('ix_analysis_tasks_heartbeat_at', ['heartbeat_at'])


def downgrade() -> None:
    with op.batch_alter_table('analysis_tasks', schema=None) as batch_op:
        batch_op.drop_index('ix_analysis_tasks_heartbeat_at')
        batch_op.drop_index('ix_analysis_tasks_owner')
        batch_op.drop_column('control_at')
        batch_op.drop_column('control')
        batch_op.drop_column('heartbeat_at')
        batch_op.drop_column('owner')
        batch_op.drop_column('result_json')
        batch_op.drop_column('progress_json')
