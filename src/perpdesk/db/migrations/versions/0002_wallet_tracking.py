"""watch-only wallet tracking

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 10:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'wallet_fills',
        sa.Column('address', sa.String(length=42), nullable=False),
        sa.Column('tid', sa.BigInteger(), nullable=False),
        sa.Column('ts', sa.BigInteger(), nullable=False),
        sa.Column('coin', sa.String(length=40), nullable=False),
        sa.Column('px', sa.Float(), nullable=False),
        sa.Column('sz', sa.Float(), nullable=False),
        sa.Column('side', sa.String(length=1), nullable=False),
        sa.Column('dir', sa.String(length=24), nullable=False),
        sa.Column('start_position', sa.Float(), nullable=False),
        sa.Column('closed_pnl', sa.Float(), nullable=False),
        sa.Column('fee', sa.Float(), nullable=False),
        sa.Column('builder_fee', sa.Float(), nullable=False),
        sa.Column('oid', sa.BigInteger(), nullable=False),
        sa.Column('crossed', sa.Boolean(), nullable=False),
        sa.Column('liquidation', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('address', 'tid'),
    )
    op.create_index('ix_wallet_fills_ts', 'wallet_fills', ['ts'])
    op.create_table(
        'wallet_funding',
        sa.Column('address', sa.String(length=42), nullable=False),
        sa.Column('coin', sa.String(length=40), nullable=False),
        sa.Column('ts', sa.BigInteger(), nullable=False),
        sa.Column('usdc', sa.Float(), nullable=False),
        sa.Column('szi', sa.Float(), nullable=False),
        sa.Column('rate', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('address', 'coin', 'ts'),
    )
    op.create_table(
        'wallet_trades',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('address', sa.String(length=42), nullable=False),
        sa.Column('open_tid', sa.BigInteger(), nullable=False),
        sa.Column('coin', sa.String(length=40), nullable=False),
        sa.Column('direction', sa.String(length=5), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('partial', sa.Boolean(), nullable=False),
        sa.Column('opened_ms', sa.BigInteger(), nullable=False),
        sa.Column('closed_ms', sa.BigInteger(), nullable=True),
        sa.Column('entry_px', sa.Float(), nullable=True),
        sa.Column('exit_px', sa.Float(), nullable=True),
        sa.Column('size', sa.Float(), nullable=False),
        sa.Column('notional', sa.Float(), nullable=False),
        sa.Column('realized_pnl', sa.Float(), nullable=False),
        sa.Column('fees', sa.Float(), nullable=False),
        sa.Column('builder_fees', sa.Float(), nullable=False),
        sa.Column('funding', sa.Float(), nullable=False),
        sa.Column('net_pnl', sa.Float(), nullable=False),
        sa.Column('n_fills', sa.Integer(), nullable=False),
        sa.Column('leverage', sa.Integer(), nullable=True),
        sa.Column('liq_px', sa.Float(), nullable=True),
        sa.Column('sl_first', sa.Float(), nullable=True),
        sa.Column('sl_px', sa.Float(), nullable=True),
        sa.Column('tp_px', sa.Float(), nullable=True),
        sa.Column('exit_reason', sa.String(length=12), nullable=True),
        sa.Column('r_multiple', sa.Float(), nullable=True),
        sa.Column('plan_id', sa.Integer(), nullable=True),
        sa.Column('plan_direction', sa.String(length=5), nullable=True),
        sa.Column('plan_grade', sa.String(length=1), nullable=True),
        sa.Column('plan_confidence', sa.Integer(), nullable=True),
        sa.Column('plan_setup', sa.String(length=40), nullable=True),
        sa.Column('followed', sa.Boolean(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_wallet_trades_address', 'wallet_trades', ['address'])
    op.create_index('ix_wallet_trades_opened_ms', 'wallet_trades', ['opened_ms'])
    op.create_index('ux_wallet_trades_open', 'wallet_trades', ['address', 'open_tid'], unique=True)


def downgrade() -> None:
    op.drop_table('wallet_trades')
    op.drop_table('wallet_funding')
    op.drop_index('ix_wallet_fills_ts', table_name='wallet_fills')
    op.drop_table('wallet_fills')
