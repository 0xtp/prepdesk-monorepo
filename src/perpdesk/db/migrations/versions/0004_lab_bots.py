"""strategy-lab bots: exit model columns on auto_trades

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17 05:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        b.add_column(sa.Column('exit_model', sa.String(length=8), nullable=False, server_default='fixed'))
        b.add_column(sa.Column('atr', sa.Float(), nullable=False, server_default='0'))
        b.add_column(sa.Column('trail_mult', sa.Float(), nullable=False, server_default='0'))
        b.add_column(sa.Column('stop_now', sa.Float(), nullable=True))
        b.add_column(sa.Column('best', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        for c in ('best', 'stop_now', 'trail_mult', 'atr', 'exit_model'):
            b.drop_column(c)
