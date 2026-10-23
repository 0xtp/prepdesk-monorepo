"""Database schema (SQLAlchemy 2). Works on SQLite (local/dev/tests) and Postgres (Supabase/Neon)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)  # "SOL" for HL perps, "dex:base:0xabc" otherwise
    symbol: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120), default="")
    hl_coin: Mapped[str | None] = mapped_column(String(40))
    hl_spot_coin: Mapped[str | None] = mapped_column(String(40))
    cg_id: Mapped[str | None] = mapped_column(String(120))
    chain: Mapped[str | None] = mapped_column(String(40))
    contract: Mapped[str | None] = mapped_column(String(120))
    pair_address: Mapped[str | None] = mapped_column(String(120))
    asset_class: Mapped[str] = mapped_column(String(40), default="Mid")
    perp_available: Mapped[bool] = mapped_column(Boolean, default=False)
    sz_decimals: Mapped[int] = mapped_column(Integer, default=2)
    max_leverage: Mapped[int] = mapped_column(Integer, default=1)
    margin_tiers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    only_isolated: Mapped[bool] = mapped_column(Boolean, default=False)
    icon_url: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FavoriteGroup(Base):
    __tablename__ = "favorite_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Favorite(Base):
    __tablename__ = "favorites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), unique=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("favorite_groups.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    asset: Mapped[Asset] = relationship(lazy="joined")
    group: Mapped[FavoriteGroup | None] = relationship(lazy="joined")


class Candle(Base):
    __tablename__ = "candles"
    series: Mapped[str] = mapped_column(String(160), primary_key=True)  # "hl:SOL", "gt:base:0xpool", "bn:SOLUSDT"
    tf: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # open time, ms
    o: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    l: Mapped[float] = mapped_column(Float)  # noqa: E741
    c: Mapped[float] = mapped_column(Float)
    v: Mapped[float] = mapped_column(Float)


class FundingRow(Base):
    __tablename__ = "funding"
    coin: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rate: Mapped[float] = mapped_column(Float)
    premium: Mapped[float] = mapped_column(Float)


class CtxSnapshot(Base):
    """Our own periodic snapshots: Hyperliquid has no open-interest history endpoint."""

    __tablename__ = "ctx_snapshots"
    coin: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mark: Mapped[float] = mapped_column(Float)
    oracle: Mapped[float] = mapped_column(Float)
    funding: Mapped[float] = mapped_column(Float)
    oi: Mapped[float] = mapped_column(Float)
    premium: Mapped[float | None] = mapped_column(Float)
    day_ntl_vlm: Mapped[float] = mapped_column(Float)


class LatestView(Base):
    """Latest rendered analysis + plan per asset (what the dashboard card shows)."""

    __tablename__ = "latest_views"
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlanRow(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    coin: Mapped[str] = mapped_column(String(40), index=True)
    asset_class: Mapped[str] = mapped_column(String(40))
    created_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    expires_ms: Mapped[int] = mapped_column(BigInteger)
    trading_tf: Mapped[str] = mapped_column(String(4))
    direction: Mapped[str] = mapped_column(String(5))
    grade: Mapped[str] = mapped_column(String(1))
    setup: Mapped[str] = mapped_column(String(40))
    setup_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[int] = mapped_column(Integer)
    expected_move: Mapped[str] = mapped_column(String(10))
    spot_only: Mapped[bool] = mapped_column(Boolean, default=False)
    entry_type: Mapped[str] = mapped_column(String(6))
    entry: Mapped[float] = mapped_column(Float)
    entry_market: Mapped[float] = mapped_column(Float)
    zone_lo: Mapped[float] = mapped_column(Float)
    zone_hi: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)
    runaway: Mapped[float] = mapped_column(Float)
    rr_tp1: Mapped[float] = mapped_column(Float)
    risk_usd: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    liq_price: Mapped[float | None] = mapped_column(Float)
    win_prob: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # factor scores, config snapshot, costs
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|open|closed|expired
    outcome: Mapped[PlanOutcome | None] = relationship(back_populates="plan", uselist=False, lazy="joined")

    __table_args__ = (Index("ix_plans_coin_created", "coin", "created_ms"),)


class PlanOutcome(Base):
    __tablename__ = "plan_outcomes"
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True)
    filled: Mapped[bool] = mapped_column(Boolean)
    fill_ms: Mapped[int | None] = mapped_column(BigInteger)
    fill_price: Mapped[float | None] = mapped_column(Float)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_reason: Mapped[str] = mapped_column(String(16))  # sl|tp2|be|timeout|not_triggered|open
    exit_ms: Mapped[int | None] = mapped_column(BigInteger)
    exit_price: Mapped[float | None] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    mfe_r: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)
    resolution: Mapped[str] = mapped_column(String(4), default="1m")
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan: Mapped[PlanRow] = relationship(back_populates="outcome")


class UserTrade(Base):
    __tablename__ = "user_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    coin: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    fill_price: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    sl: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float | None] = mapped_column(Float)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, default="")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    coins: Mapped[list[Any]] = mapped_column(JSON)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON)
    report_html: Mapped[str] = mapped_column(Text)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    setup: Mapped[str] = mapped_column(String(40))
    coin: Mapped[str] = mapped_column(String(40))
    asset_class: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    ts: Mapped[int] = mapped_column(BigInteger)
    confidence: Mapped[int] = mapped_column(Integer)
    grade_ex_gate: Mapped[str] = mapped_column(String(1))
    rr_tp1: Mapped[float] = mapped_column(Float)
    r_multiple: Mapped[float] = mapped_column(Float)
    tp1_before_sl: Mapped[bool] = mapped_column(Boolean)
    period: Mapped[str] = mapped_column(String(8))  # train|oos|holdout


class SetupStat(Base):
    __tablename__ = "setup_stats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    setup: Mapped[str] = mapped_column(String(40))
    scope_kind: Mapped[str] = mapped_column(String(8))  # asset|class|all
    scope: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON)  # n, win, wilson_lo, avg_rr, be_wr, expectancy, pf, max_dd, ...
    gate_passed: Mapped[bool] = mapped_column(Boolean)
    gate_reasons: Mapped[list[Any]] = mapped_column(JSON)
    calibration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    live_demoted: Mapped[bool] = mapped_column(Boolean, default=False)
    live_reason: Mapped[str | None] = mapped_column(Text)


class SettingOverride(Base):
    __tablename__ = "setting_overrides"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"v": value}
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceHealthRow(Base):
    __tablename__ = "source_health"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(BigInteger, default=0)
    errors: Mapped[int] = mapped_column(BigInteger, default=0)


class JobHeartbeat(Base):
    __tablename__ = "job_heartbeats"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_run: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean)
    duration_ms: Mapped[int] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")
    cursor: Mapped[int] = mapped_column(Integer, default=0)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    model: Mapped[str] = mapped_column(String(60))
    purpose: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class LlmCache(Base):
    __tablename__ = "llm_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KV(Base):
    """Small shared cache across serverless instances (fear & greed, CME, macro, calendar, org cost)."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertState(Base):
    __tablename__ = "alert_state"
    coin: Mapped[str] = mapped_column(String(40), primary_key=True)
    direction: Mapped[str] = mapped_column(String(5))
    grade: Mapped[str] = mapped_column(String(1))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WalletFill(Base):
    """Raw Hyperliquid fills of the watched address (read-only sync)."""

    __tablename__ = "wallet_fills"
    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    tid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    coin: Mapped[str] = mapped_column(String(40))
    px: Mapped[float] = mapped_column(Float)
    sz: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String(1))  # B|A
    dir: Mapped[str] = mapped_column(String(24))
    start_position: Mapped[float] = mapped_column(Float)
    closed_pnl: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)  # exchange fee (negative = maker rebate)
    builder_fee: Mapped[float] = mapped_column(Float, default=0.0)  # front-end fee, e.g. Phantom's
    oid: Mapped[int] = mapped_column(BigInteger)
    crossed: Mapped[bool] = mapped_column(Boolean)
    liquidation: Mapped[bool] = mapped_column(Boolean, default=False)


class WalletFunding(Base):
    __tablename__ = "wallet_funding"
    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    coin: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usdc: Mapped[float] = mapped_column(Float)
    szi: Mapped[float] = mapped_column(Float)
    rate: Mapped[float] = mapped_column(Float)


class WalletTrade(Base):
    """Round-trip trade rebuilt from fills, enriched with the stop/target seen while it was open and the
    app plan that was live when it was opened."""

    __tablename__ = "wallet_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(42), index=True)
    open_tid: Mapped[int] = mapped_column(BigInteger)
    coin: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(8))  # open|closed
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    closed_ms: Mapped[int | None] = mapped_column(BigInteger)
    entry_px: Mapped[float | None] = mapped_column(Float)
    exit_px: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)  # exchange + builder fees
    builder_fees: Mapped[float] = mapped_column(Float, default=0.0)
    funding: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    n_fills: Mapped[int] = mapped_column(Integer, default=0)
    leverage: Mapped[int | None] = mapped_column(Integer)
    liq_px: Mapped[float | None] = mapped_column(Float)
    sl_first: Mapped[float | None] = mapped_column(Float)  # first stop seen while open (defines R)
    sl_px: Mapped[float | None] = mapped_column(Float)  # latest stop seen
    tp_px: Mapped[float | None] = mapped_column(Float)  # latest nearest target seen
    exit_reason: Mapped[str | None] = mapped_column(String(12))  # target|stop|manual|liquidated|closed
    r_multiple: Mapped[float | None] = mapped_column(Float)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    plan_direction: Mapped[str | None] = mapped_column(String(5))
    plan_grade: Mapped[str | None] = mapped_column(String(1))
    plan_confidence: Mapped[int | None] = mapped_column(Integer)
    plan_setup: Mapped[str | None] = mapped_column(String(40))
    followed: Mapped[bool | None] = mapped_column(Boolean)  # took the app's direction
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ux_wallet_trades_open", "address", "open_tid", unique=True),)


class AutoTrade(Base):
    """Trades taken by the auto-trader (Phase 2). mode=paper rows are simulated against live 1m candles."""

    __tablename__ = "auto_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)  # paper|live|dry (dry run)
    coin: Mapped[str] = mapped_column(String(40), index=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    setup: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(10), index=True)  # pending|open|closed|cancelled|error
    signal_ms: Mapped[int] = mapped_column(BigInteger)  # close time of the 4h bar that triggered it
    created_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    entry_type: Mapped[str] = mapped_column(String(6))
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)
    runaway: Mapped[float] = mapped_column(Float)
    expires_ms: Mapped[int] = mapped_column(BigInteger)
    max_hold_ms: Mapped[int] = mapped_column(BigInteger)
    qty: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    notional: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    risk_usd: Mapped[float] = mapped_column(Float)
    liq_price: Mapped[float | None] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float)  # rates
    exit_fee: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float)
    funding_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    fill_ms: Mapped[int | None] = mapped_column(BigInteger)
    fill_px: Mapped[float | None] = mapped_column(Float)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_reason: Mapped[str | None] = mapped_column(String(16))
    exit_ms: Mapped[int | None] = mapped_column(BigInteger)
    exit_px: Mapped[float | None] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    pnl_usd: Mapped[float | None] = mapped_column(Float)
    client_id: Mapped[str] = mapped_column(String(40), unique=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # lab bots: exit model and its state (fixed = TP1/TP2/break-even; trail = chandelier; carry = funding)
    exit_model: Mapped[str] = mapped_column(String(8), default="fixed", server_default="fixed")
    atr: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    trail_mult: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    stop_now: Mapped[float | None] = mapped_column(Float)
    best: Mapped[float | None] = mapped_column(Float)
    # would the coin card have shown TAKE when the bot opened this? (None = opened before tagging existed)
    take: Mapped[bool | None] = mapped_column(Boolean)
    take_reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    alert_open_ms: Mapped[int | None] = mapped_column(BigInteger)  # Telegram alert sent (dedupe)
    alert_close_ms: Mapped[int | None] = mapped_column(BigInteger)
    alert_fill_ms: Mapped[int | None] = mapped_column(BigInteger)  # limit entry filled
    alert_tp1_ms: Mapped[int | None] = mapped_column(BigInteger)  # TP1 hit: take half, stop to entry
    # mode=live|dry: the paper trade it copies, the order ids (cloids) on the exchange and the current stop
    exec_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
