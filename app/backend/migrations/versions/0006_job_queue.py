"""job queue: durable background jobs with retries, and executor heartbeats

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('queued', 'running') AND dedupe_key IS NOT NULL"


def upgrade() -> None:
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('payload_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('run_after', sa.DateTime(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=128), nullable=True),
    sa.Column('locked_by', sa.String(length=128), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.Column('result_json', sa.JSON(), nullable=True),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_kind'), 'jobs', ['kind'], unique=False)
    op.create_index('ix_jobs_claim', 'jobs', ['status', 'run_after'], unique=False)
    op.create_index('uq_job_active_dedupe', 'jobs', ['dedupe_key'], unique=True,
                    sqlite_where=sa.text(_ACTIVE), postgresql_where=sa.text(_ACTIVE))
    op.create_table('job_workers',
    sa.Column('id', sa.String(length=128), nullable=False),
    sa.Column('threads', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('job_workers')
    op.drop_index('uq_job_active_dedupe', table_name='jobs')
    op.drop_index('ix_jobs_claim', table_name='jobs')
    op.drop_index(op.f('ix_jobs_kind'), table_name='jobs')
    op.drop_table('jobs')
