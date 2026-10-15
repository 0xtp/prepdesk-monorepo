"""Named rule-based setups. Few parameters, round numbers, each with a tiny grid for walk-forward.

A setup only *qualifies* a call; the call's direction always comes from the composite score.
A setup that fires in the opposite direction is reported as a note, not traded.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from perpdesk.analysis.structure import detect_range
from perpdesk.analysis.types import Direction, SetupSignal

# name -> parameter grid explored by walk-forward (kept deliberately tiny)
PARAM_GRIDS: dict[str, list[dict[str, float]]] = {
    "trend_pullback": [{"ema": 20.0}, {"ema": 50.0}],
    "range_reclaim": [{"lookback": 48.0}, {"lookback": 96.0}],
    "breakout": [{"lookback": 20.0}, {"lookback": 40.0}],
    "funding_fade": [{"z": 2.0}, {"z": 2.5}],
}
DEFAULT_PARAMS: dict[str, dict[str, float]] = {k: v[0] for k, v in PARAM_GRIDS.items()}
SETUP_LABELS = {
    "trend_pullback": "Trend pullback",
    "range_reclaim": "Range-edge reclaim",
    "breakout": "Breakout with volume",
    "funding_fade": "Funding-crowded fade",
    "composite": "Composite bias",
}


@dataclass
class SetupContext:
    trading: pd.DataFrame  # trading TF with features
    h4: pd.DataFrame | None
    d1: pd.DataFrame | None
    funding_z: float
    atr: float


def _f(df: pd.DataFrame, col: str, i: int = -1) -> float:
    v = df[col].iloc[i]
    return float(v) if pd.notna(v) else float("nan")


def _htf_trend(ctx: SetupContext) -> int:
    """+1 if 1D and 4H both up (close > EMA50 and EMA20 > EMA50), -1 if both down, else 0."""
    votes = []
    for df in (ctx.d1, ctx.h4):
        if df is None or len(df) < 55:
            return 0
        c, e20, e50 = _f(df, "close"), _f(df, "ema20"), _f(df, "ema50")
        if not (math.isfinite(e20) and math.isfinite(e50)):
            return 0
        votes.append(1 if (c > e50 and e20 > e50) else (-1 if (c < e50 and e20 < e50) else 0))
    if all(v == 1 for v in votes):
        return 1
    if all(v == -1 for v in votes):
        return -1
    return 0


def trend_pullback(ctx: SetupContext, p: dict[str, float]) -> SetupSignal | None:
    df = ctx.trading
    if len(df) < 60:
        return None
    trend = _htf_trend(ctx)
    if trend == 0:
        return None
    col = "ema20" if p["ema"] == 20 else "ema50"
    w = df.iloc[-3:]
    c, o = _f(df, "close"), _f(df, "open")
    ema_now = _f(df, col)
    if not math.isfinite(ema_now):
        return None
    if trend > 0:
        touched = bool((w["low"] <= w[col]).any())
        if touched and c > ema_now and c > o:
            return SetupSignal("trend_pullback", "LONG", "limit", dict(p), f"pullback to EMA{int(p['ema'])} in 1D+4H uptrend")
    else:
        touched = bool((w["high"] >= w[col]).any())
        if touched and c < ema_now and c < o:
            return SetupSignal("trend_pullback", "SHORT", "limit", dict(p),
                               f"pullback to EMA{int(p['ema'])} in 1D+4H downtrend")
    return None


def range_reclaim(ctx: SetupContext, p: dict[str, float]) -> SetupSignal | None:
    df = ctx.trading
    lb = int(p["lookback"])
    if len(df) < lb + 5:
        return None
    # range defined on bars before the last 3 so the sweep itself doesn't redefine it
    rng = detect_range(df.iloc[:-3], ctx.atr, lookback=lb)
    if rng is None:
        return None
    w = df.iloc[-3:]
    c = _f(df, "close")
    if (w["low"] < rng.low).any() and rng.low < c < rng.mid:
        return SetupSignal("range_reclaim", "LONG", "limit", dict(p), f"swept range low {rng.low:.6g} and reclaimed")
    if (w["high"] > rng.high).any() and rng.mid < c < rng.high:
        return SetupSignal("range_reclaim", "SHORT", "limit", dict(p), f"swept range high {rng.high:.6g} and rejected")
    return None


def breakout(ctx: SetupContext, p: dict[str, float]) -> SetupSignal | None:
    df = ctx.trading
    lb = int(p["lookback"])
    if len(df) < lb + 25:
        return None
    prior = df.iloc[-(lb + 1) : -1]
    c, v, vma = _f(df, "close"), _f(df, "volume"), _f(df, "vol_ma20")
    if not (math.isfinite(vma) and vma > 0) or v < 1.5 * vma:
        return None
    trend = _htf_trend(ctx)
    if c > float(prior["high"].max()) and trend >= 0:
        return SetupSignal("breakout", "LONG", "market", dict(p), f"close above {lb}-bar high on {v / vma:.1f}× volume")
    if c < float(prior["low"].min()) and trend <= 0:
        return SetupSignal("breakout", "SHORT", "market", dict(p), f"close below {lb}-bar low on {v / vma:.1f}× volume")
    return None


def funding_fade(ctx: SetupContext, p: dict[str, float]) -> SetupSignal | None:
    df = ctx.trading
    if len(df) < 30 or not math.isfinite(ctx.funding_z) or ctx.atr <= 0:
        return None
    c, e20, r = _f(df, "close"), _f(df, "ema20"), _f(df, "rsi")
    ext = (c - e20) / ctx.atr
    if ctx.funding_z >= p["z"] and ext >= 2 and r >= 70:
        note = f"funding z {ctx.funding_z:+.1f}, {ext:.1f} ATR extended"
        return SetupSignal("funding_fade", "SHORT", "limit", dict(p), note)
    if ctx.funding_z <= -p["z"] and ext <= -2 and r <= 30:
        note = f"funding z {ctx.funding_z:+.1f}, {abs(ext):.1f} ATR extended"
        return SetupSignal("funding_fade", "LONG", "limit", dict(p), note)
    return None


SETUPS: dict[str, Callable[[SetupContext, dict[str, float]], SetupSignal | None]] = {
    "trend_pullback": trend_pullback,
    "range_reclaim": range_reclaim,
    "breakout": breakout,
    "funding_fade": funding_fade,
}


def detect_setups(ctx: SetupContext, params: dict[str, dict[str, float]] | None = None) -> list[SetupSignal]:
    params = params or {}
    out = []
    for name, fn in SETUPS.items():
        sig = fn(ctx, params.get(name, DEFAULT_PARAMS[name]))
        if sig is not None:
            out.append(sig)
    return out


def choose_setup(signals: list[SetupSignal], direction: Direction, sideways: bool) -> tuple[SetupSignal, list[str]]:
    """Pick the setup matching the call direction; others become notes."""
    notes = []
    match = [s for s in signals if s.direction == direction]
    for s in signals:
        if s.direction != direction:
            notes.append(f"Opposing setup present: {SETUP_LABELS[s.name]} {s.direction} ({s.note})")
    priority = ["breakout", "trend_pullback", "range_reclaim", "funding_fade"]
    match.sort(key=lambda s: priority.index(s.name))
    if match:
        return match[0], notes
    return SetupSignal("composite", direction, "limit", {}, "range trade" if sideways else "composite bias"), notes
