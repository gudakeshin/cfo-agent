"""ask messages: the CFO's conversation about a whole briefing

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ask_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('digest_id', sa.Integer(), nullable=True),
    sa.Column('user_email', sa.String(length=256), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('citations_json', sa.JSON(), nullable=True),
    sa.Column('verification_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['digest_id'], ['digests.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ask_messages_digest_id'), 'ask_messages', ['digest_id'], unique=False)
    op.create_index(op.f('ix_ask_messages_user_email'), 'ask_messages', ['user_email'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ask_messages_user_email'), table_name='ask_messages')
    op.drop_index(op.f('ix_ask_messages_digest_id'), table_name='ask_messages')
    op.drop_table('ask_messages')
