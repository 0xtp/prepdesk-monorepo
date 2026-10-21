"""auto_trades: markers for the fill and TP1 follow-up alerts

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 12:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        b.add_column(sa.Column('alert_fill_ms', sa.BigInteger(), nullable=True))
        b.add_column(sa.Column('alert_tp1_ms', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        b.drop_column('alert_tp1_ms')
        b.drop_column('alert_fill_ms')
