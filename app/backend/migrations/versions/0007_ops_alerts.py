"""operator alerts: state for failure alerting

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 18:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ops_alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=96), nullable=False),
    sa.Column('severity', sa.String(length=12), nullable=False),
    sa.Column('title', sa.String(length=256), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_notified_at', sa.DateTime(), nullable=True),
    sa.Column('notify_count', sa.Integer(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ops_alerts_key'), 'ops_alerts', ['key'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ops_alerts_key'), table_name='ops_alerts')
    op.drop_table('ops_alerts')
