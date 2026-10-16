"""auto-trader ledger (paper first)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 18:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'auto_trades',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=8), nullable=False),
        sa.Column('coin', sa.String(length=40), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=True),
        sa.Column('setup', sa.String(length=40), nullable=False),
        sa.Column('direction', sa.String(length=5), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('signal_ms', sa.BigInteger(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('entry_type', sa.String(length=6), nullable=False),
        sa.Column('entry', sa.Float(), nullable=False),
        sa.Column('sl', sa.Float(), nullable=False),
        sa.Column('tp1', sa.Float(), nullable=False),
        sa.Column('tp2', sa.Float(), nullable=False),
        sa.Column('runaway', sa.Float(), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
        sa.Column('max_hold_ms', sa.BigInteger(), nullable=False),
        sa.Column('qty', sa.Float(), nullable=False),
        sa.Column('leverage', sa.Integer(), nullable=False),
        sa.Column('notional', sa.Float(), nullable=False),
        sa.Column('margin', sa.Float(), nullable=False),
        sa.Column('risk_usd', sa.Float(), nullable=False),
        sa.Column('liq_price', sa.Float(), nullable=True),
        sa.Column('entry_fee', sa.Float(), nullable=False),
        sa.Column('exit_fee', sa.Float(), nullable=False),
        sa.Column('slippage', sa.Float(), nullable=False),
        sa.Column('funding_per_hour', sa.Float(), nullable=False),
        sa.Column('confidence', sa.Integer(), nullable=False),
        sa.Column('fill_ms', sa.BigInteger(), nullable=True),
        sa.Column('fill_px', sa.Float(), nullable=True),
        sa.Column('tp1_hit', sa.Boolean(), nullable=False),
        sa.Column('exit_reason', sa.String(length=16), nullable=True),
        sa.Column('exit_ms', sa.BigInteger(), nullable=True),
        sa.Column('exit_px', sa.Float(), nullable=True),
        sa.Column('r_multiple', sa.Float(), nullable=True),
        sa.Column('pnl_usd', sa.Float(), nullable=True),
        sa.Column('client_id', sa.String(length=40), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_id'),
    )
    op.create_index('ix_auto_trades_mode', 'auto_trades', ['mode'])
    op.create_index('ix_auto_trades_coin', 'auto_trades', ['coin'])
    op.create_index('ix_auto_trades_status', 'auto_trades', ['status'])
    op.create_index('ix_auto_trades_created_ms', 'auto_trades', ['created_ms'])


def downgrade() -> None:
    op.drop_table('auto_trades')
