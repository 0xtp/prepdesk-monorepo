"""The analysis engine: MarketSnapshot -> Analysis. Pure and deterministic.

The same function runs live (worker / on-demand refresh) and in the backtester.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from perpdesk.analysis import factors as fx
from perpdesk.analysis import structure as st
from perpdesk.analysis.cme_gap import find_gaps
from perpdesk.analysis.indicators import ensure_features, session_vwap, volume_profile
from perpdesk.analysis.setups import SetupContext, choose_setup, detect_setups
from perpdesk.analysis.types import (
    Analysis,
    Direction,
    Factor,
    Flag,
    Level,
    MarketSnapshot,
    Move,
    Pivot,
)

STRUCTURE_WINDOW = 300
NEAR_ZERO = 0.05


@dataclass(frozen=True)
class EngineConfig:
    tf_weights: dict[str, float] = field(
        default_factory=lambda: {"1w": 0.30, "1d": 0.25, "4h": 0.20, "1h": 0.15, "15m": 0.10}
    )
    group_weights: dict[str, float] = field(
        default_factory=lambda: {"trend": 0.30, "momentum": 0.15, "structure": 0.20, "derivatives": 0.15,
                                 "regime": 0.15, "sentiment": 0.05, "macro": 0.0}
    )
    trading_tf: str = "1h"
    max_atr_pctl: float = 95.0
    event_blackout_hours: float = 2.0
    min_liquidity_usd: float = 2_000_000.0
    setup_params: dict[str, dict[str, float]] = field(default_factory=dict)
    disabled_groups: frozenset[str] = frozenset()  # for factor-ablation backtests
    # |score| at percentiles 0..100 from the latest backtest. When present, confidence = the percentile
    # of today's signal strength versus history ("stronger than N% of past signals").
    score_quantiles: tuple[float, ...] = ()


def _tail(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.iloc[-n:] if len(df) > n else df


def analyse(snap: MarketSnapshot, cfg: EngineConfig) -> Analysis:
    frames = {tf: ensure_features(df) for tf, df in snap.candles.items() if df is not None and len(df) > 0}
    if cfg.trading_tf not in frames or len(frames[cfg.trading_tf]) < 60:
        raise ValueError(f"{snap.coin}: not enough {cfg.trading_tf} candles to analyse")
    trading = _tail(frames[cfg.trading_tf], STRUCTURE_WINDOW).copy()
    trading["vwap"] = session_vwap(trading)
    atrv = float(trading["atr"].iloc[-1])
    atr_pctl = float(trading["atr_pctl"].iloc[-1]) if pd.notna(trading["atr_pctl"].iloc[-1]) else 50.0
    price = snap.price
    d1, w1, h4 = frames.get("1d"), frames.get("1w"), frames.get("4h")

    # ---- structure
    pivots = st.find_pivots(trading, 3)
    levels: list[Level] = st.cluster_levels(pivots, 0.5 * atrv, price)
    htf_pivots: dict[str, list[Pivot]] = {}
    for tf in ("4h", "1d"):
        df = frames.get(tf)
        if df is not None and len(df) >= 30:
            pv = st.find_pivots(_tail(df, STRUCTURE_WINDOW), 3)
            htf_pivots[tf] = pv
            if tf == "4h":
                levels += [Level(lv.price, lv.kind, lv.strength + 1, lv.label + " (4H)")
                           for lv in st.cluster_levels(pv, 0.5 * atrv, price)]
    levels += st.equal_extremes(pivots, trading, 0.1 * atrv)
    levels += st.prior_extremes(d1, w1)
    levels += st.fair_value_gaps(trading)
    vw = float(trading["vwap"].iloc[-1])
    levels.append(Level(vw, "vwap", 1, "session VWAP"))
    if len(trading) >= 50:
        poc, vah, val = volume_profile(_tail(trading, 200))
        levels += [Level(poc, "poc", 2, "volume POC"), Level(vah, "vah", 1, "value area high"),
                   Level(val, "val", 1, "value area low")]
    for n in (20, 50, 200):
        v = trading[f"ema{n}"].iloc[-1]
        if pd.notna(v):
            levels.append(Level(float(v), "ema", 1, f"EMA{n} ({cfg.trading_tf})"))
    levels += snap.liquidation_levels
    rng = st.detect_range(trading, atrv)

    # ---- factors
    factors: list[Factor] = []
    for tf, df in frames.items():
        if tf not in cfg.tf_weights:
            continue
        factors += fx.trend_factors(tf, df)
        piv = pivots if tf == cfg.trading_tf else htf_pivots.get(tf) or st.find_pivots(_tail(df, STRUCTURE_WINDOW), 3)
        factors += fx.momentum_factors(tf, _tail(df, STRUCTURE_WINDOW), piv)
        if tf in ("1d", "4h"):
            f = fx.structure_trend_factor(tf, htf_pivots.get(tf, []))
            if f:
                factors.append(f)
    if w1 is not None:
        f = fx.weekly_ma_factor(w1)
        if f:
            factors.append(f)
    factors += fx.volatility_factors(cfg.trading_tf, trading)
    factors += fx.levels_factors(cfg.trading_tf, trading, levels, atrv)
    if snap.meta.is_perp:
        factors += fx.derivatives_factors(snap.funding, snap.current_funding, snap.current_premium, snap.oi, price)
        lf = fx.liquidation_factor(snap.liquidation_levels, price)
        if lf:
            factors.append(lf)
    btc_regime = 0
    rs, corr = float("nan"), float("nan")
    if snap.btc_candles:
        btc_frames = {tf: ensure_features(df) for tf, df in snap.btc_candles.items() if df is not None and len(df)}
        f, btc_regime = fx.btc_regime_factor(btc_frames)
        if f:
            factors.append(f)
        if d1 is not None and "1d" in btc_frames:
            rs, corr = fx.relative_strength(d1, btc_frames["1d"])
            f = fx.relative_strength_factor(rs, corr)
            if f:
                factors.append(f)
    f = fx.fear_greed_factor(snap.fear_greed)
    if f:
        factors.append(f)
    gaps = []
    if snap.cme_daily is not None:
        btc_h = snap.candles.get("1h") if snap.coin == "BTC" else (snap.btc_candles or {}).get("1h")
        gaps = find_gaps(snap.cme_daily, btc_h, price if snap.coin == "BTC" else float(btc_h["close"].iloc[-1])
                         if btc_h is not None and len(btc_h) else price)
        f = fx.cme_gap_factor(gaps)
        if f:
            factors.append(f)
    f = fx.macro_factor(snap.macro)
    if f:
        factors.append(f)
    if cfg.disabled_groups:
        factors = [Factor(x.name, x.group, x.tf, x.score, x.reason + " (ablated)", x.source, x.as_of, False)
                   if x.group in cfg.disabled_groups else x for x in factors]

    # ---- score
    group_scores, contributions, score = combine(factors, cfg)
    htf_dir = htf_direction(frames)
    near_zero = abs(score) < NEAR_ZERO
    direction: Direction = htf_dir if near_zero else ("LONG" if score > 0 else "SHORT")
    confidence = confidence_from(score, group_scores, cfg, direction)
    if near_zero:
        confidence = min(confidence, 20)
    dsign = 1 if direction == "LONG" else -1

    ranked = sorted(contributions, key=lambda fc: fc[1] * dsign, reverse=True)
    top_for = [f for f, c in ranked if c * dsign > 0][:3]
    top_against = [f for f, c in reversed(ranked) if c * dsign < 0][:3]

    # ---- expected move
    adx_v = trading["adx"].iloc[-1]
    bwp = trading["bb_bw_pctl"].iloc[-1]
    sideways = rng is not None or (pd.notna(adx_v) and adx_v < 20 and pd.notna(bwp) and bwp < 30)
    expected: Move = "SIDEWAYS" if sideways else ("UP" if direction == "LONG" else "DOWN")

    # ---- flags
    flags: list[Flag] = []
    if atr_pctl > cfg.max_atr_pctl:
        flags.append(Flag("volatility", f"extreme volatility: ATR at {atr_pctl:.0f}th percentile"))
    asof_dt = snap.asof.to_pydatetime()
    ev = fx.upcoming_events(snap.events, asof_dt, cfg.event_blackout_hours)
    if ev:
        e = ev[0]
        flags.append(Flag("event", f"{e.title} ({e.country}) at {e.at:%a %H:%M} UTC, within {cfg.event_blackout_hours:g}h"))
    if snap.stale_sources:
        flags.append(Flag("stale", "stale data: " + ", ".join(snap.stale_sources)))
    liq_val = snap.liquidity_usd if snap.liquidity_usd is not None else snap.day_volume_usd
    if liq_val is not None and liq_val < cfg.min_liquidity_usd:
        flags.append(Flag("liquidity", f"low liquidity: ${liq_val:,.0f} < ${cfg.min_liquidity_usd:,.0f}"))
    if snap.coin != "BTC" and btc_regime != 0 and dsign != btc_regime:
        strong_rs = math.isfinite(rs) and rs * dsign > 0.10
        if not strong_rs:
            flags.append(Flag("btc_counter",
                              f"{direction.lower()} against a strong BTC {'downtrend' if btc_regime < 0 else 'uptrend'}"))
        else:
            flags.append(Flag("btc_counter", f"against BTC trend but relative strength {rs * 100:+.0f}% (flagged)",
                              reduces_size=False))
    fz, _ = fx.funding_zscore(snap.funding, snap.current_funding)
    crowd = fx.crowding(dsign, fz, price, float(trading["ema20"].iloc[-1]), atrv)
    if crowd and snap.meta.is_perp:
        flags.append(Flag("crowded", crowd))

    # ---- setup
    ctx = SetupContext(trading, h4, d1, fz, atrv)
    signals = detect_setups(ctx, cfg.setup_params)
    setup, notes = choose_setup(signals, direction, sideways)

    return Analysis(
        coin=snap.coin, asof=snap.asof, price=price, trading_tf=cfg.trading_tf, factors=factors,
        group_scores=group_scores, score=score, direction=direction, confidence=confidence,
        expected_move=expected, setup=setup, atr=atrv, atr_pctl=atr_pctl, levels=levels, pivots=pivots,
        range=rng, flags=flags, top_for=top_for, top_against=top_against, htf_direction=htf_dir,
        cme_gaps=gaps, near_zero=near_zero, notes=notes,
    )


def combine(factors: list[Factor], cfg: EngineConfig) -> tuple[dict[str, float], list[tuple[Factor, float]], float]:
    """Weighted sum: within a group, factors are averaged with TF weights; groups by config weight.
    Returns group scores (-2..2), per-factor contribution to the final score, final score (-1..1)."""
    by_group: dict[str, list[tuple[Factor, float]]] = {}
    for f in factors:
        if not f.scored:
            continue
        w = cfg.tf_weights.get(f.tf, 1.0) if f.tf != "-" else 1.0
        by_group.setdefault(f.group, []).append((f, w))
    group_scores: dict[str, float] = {}
    gw_total = sum(cfg.group_weights.get(g, 0.0) for g in by_group)
    contributions: list[tuple[Factor, float]] = []
    total = 0.0
    for g, items in by_group.items():
        wsum = sum(w for _, w in items)
        gs = sum(f.score * w for f, w in items) / wsum if wsum else 0.0
        group_scores[g] = gs
        gw = cfg.group_weights.get(g, 0.0)
        if gw_total <= 0 or gw <= 0:
            continue
        for f, w in items:
            c = (gw / gw_total) * (w / wsum) * f.score / 2.0
            contributions.append((f, c))
            total += c
    return group_scores, contributions, max(-1.0, min(1.0, total))


def confidence_from(score: float, group_scores: dict[str, float], cfg: EngineConfig, direction: Direction) -> int:
    if len(cfg.score_quantiles) >= 2:
        return percentile_confidence(abs(score), cfg.score_quantiles)
    dsign = 1 if direction == "LONG" else -1
    active = {g: s for g, s in group_scores.items() if abs(s) > 0.1 and cfg.group_weights.get(g, 0) > 0}
    wtot = sum(cfg.group_weights[g] for g in active)
    agree = sum(cfg.group_weights[g] for g, s in active.items() if s * dsign > 0) / wtot if wtot else 0.5
    strength = min(1.0, abs(score) / 0.5)
    return round(100 * strength * (0.5 + 0.5 * agree))


def htf_direction(frames: dict[str, pd.DataFrame]) -> Direction:
    for tf in ("1d", "1w", "4h"):
        df = frames.get(tf)
        if df is None or len(df) < 20:
            continue
        c = float(df["close"].iloc[-1])
        for col in ("ema200", "ema50", "ema20"):
            v = df[col].iloc[-1]
            if pd.notna(v):
                return "LONG" if c >= float(v) else "SHORT"
    return "LONG"


def percentile_confidence(abs_score: float, quantiles: tuple[float, ...]) -> int:
    """Percentile (0-100) of a signal strength within a reference distribution of |score|."""
    q = list(quantiles)
    n = len(q) - 1
    if abs_score <= q[0]:
        return 0
    if abs_score >= q[-1]:
        return 100
    for i in range(n):
        lo, hi = q[i], q[i + 1]
        if lo <= abs_score <= hi:
            frac = 0.0 if hi <= lo else (abs_score - lo) / (hi - lo)
            return round((i + frac) * 100 / n)
    return 100
