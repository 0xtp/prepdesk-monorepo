"""auto_trades: TAKE tag at entry and Telegram alert markers

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20 03:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        b.add_column(sa.Column('take', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('take_reason', sa.Text(), nullable=False, server_default=''))
        b.add_column(sa.Column('alert_open_ms', sa.BigInteger(), nullable=True))
        b.add_column(sa.Column('alert_close_ms', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('auto_trades') as b:
        for c in ('alert_close_ms', 'alert_open_ms', 'take_reason', 'take'):
            b.drop_column(c)
