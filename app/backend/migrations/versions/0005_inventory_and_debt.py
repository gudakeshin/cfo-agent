"""inventory and debt balances: feeds for the cash cycle and leverage metrics

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('erp_inventory',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('business_key', sa.String(length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('category', sa.String(length=64), nullable=False),
    sa.Column('entity', sa.String(length=32), nullable=False),
    sa.Column('value_paise', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_erp_inventory_business_key'), 'erp_inventory', ['business_key'], unique=True)
    op.create_index(op.f('ix_erp_inventory_as_of_date'), 'erp_inventory', ['as_of_date'], unique=False)
    op.create_index(op.f('ix_erp_inventory_entity'), 'erp_inventory', ['entity'], unique=False)
    op.create_table('erp_debt',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('business_key', sa.String(length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('facility', sa.String(length=64), nullable=False),
    sa.Column('lender', sa.String(length=128), nullable=False),
    sa.Column('entity', sa.String(length=32), nullable=False),
    sa.Column('outstanding_paise', sa.BigInteger(), nullable=False),
    sa.Column('interest_rate_pct', sa.Float(), nullable=False),
    sa.Column('principal_due_90d_paise', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_erp_debt_business_key'), 'erp_debt', ['business_key'], unique=True)
    op.create_index(op.f('ix_erp_debt_as_of_date'), 'erp_debt', ['as_of_date'], unique=False)
    op.create_index(op.f('ix_erp_debt_facility'), 'erp_debt', ['facility'], unique=False)
    op.create_index(op.f('ix_erp_debt_entity'), 'erp_debt', ['entity'], unique=False)


def downgrade() -> None:
    for t in ('erp_debt', 'erp_inventory'):
        for col in (['entity', 'facility', 'as_of_date', 'business_key'] if t == 'erp_debt' else ['entity', 'as_of_date', 'business_key']):
            op.drop_index(op.f(f'ix_{t}_{col}'), table_name=t)
        op.drop_table(t)
