"""auto_trades: exec_state for live (and dry-run) trades

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23 12:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        b.add_column(sa.Column('exec_state', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        b.drop_column('exec_state')
