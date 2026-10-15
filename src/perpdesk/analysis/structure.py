"""Market structure: swing pivots, support/resistance clusters, liquidity pools, ranges, FVGs.

A pivot at bar i needs `n` bars on each side, so it is only *known* at bar i+n. Every function
here only returns pivots confirmed by the last bar of the frame it receives (no lookahead).
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from perpdesk.analysis.types import Level, Pivot, RangeInfo


def find_pivots(df: pd.DataFrame, n: int = 3) -> list[Pivot]:
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    out: list[Pivot] = []
    last_confirmable = len(df) - 1 - n
    for i in range(n, last_confirmable + 1):
        hw = highs[i - n : i + n + 1]
        lw = lows[i - n : i + n + 1]
        if highs[i] == hw.max() and (hw[:n] < highs[i]).all():
            out.append(Pivot(i, pd.Timestamp(df.index[i]), float(highs[i]), "high"))
        if lows[i] == lw.min() and (lw[:n] > lows[i]).all():
            out.append(Pivot(i, pd.Timestamp(df.index[i]), float(lows[i]), "low"))
    return out


def market_structure(pivots: list[Pivot]) -> tuple[int, str]:
    """+1 for higher highs & higher lows, -1 for lower highs & lower lows, 0 otherwise."""
    hs = [p.price for p in pivots if p.kind == "high"][-2:]
    ls = [p.price for p in pivots if p.kind == "low"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return 0, "not enough swings"
    hh, hl = hs[1] > hs[0], ls[1] > ls[0]
    lh, ll = hs[1] < hs[0], ls[1] < ls[0]
    if hh and hl:
        return 1, "higher highs and higher lows"
    if lh and ll:
        return -1, "lower highs and lower lows"
    return 0, "mixed swings (" + ("HH" if hh else "LH") + "/" + ("HL" if hl else "LL") + ")"


def cluster_levels(pivots: list[Pivot], tol: float, price: float) -> list[Level]:
    """Group pivot prices within `tol` into levels; label support/resistance relative to price."""
    if not pivots or tol <= 0:
        return []
    pts = sorted(p.price for p in pivots)
    clusters: list[list[float]] = [[pts[0]]]
    for x in pts[1:]:
        if x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    out = []
    for cl in clusters:
        lvl = float(np.mean(cl))
        kind = "support" if lvl < price else "resistance"
        out.append(Level(lvl, kind, len(cl), f"{kind} ×{len(cl)}"))
    return out


def equal_extremes(pivots: list[Pivot], df: pd.DataFrame, tol: float) -> list[Level]:
    """Untested equal highs / equal lows (liquidity pools): two+ pivots within tol, never exceeded since."""
    out: list[Level] = []
    for kind, label in (("high", "eqh"), ("low", "eql")):
        ps = [p for p in pivots if p.kind == kind][-12:]
        used: set[int] = set()
        for i, a in enumerate(ps):
            if i in used:
                continue
            group = [a]
            for j in range(i + 1, len(ps)):
                if abs(ps[j].price - a.price) <= tol:
                    group.append(ps[j])
                    used.add(j)
            if len(group) < 2:
                continue
            lvl = max(p.price for p in group) if kind == "high" else min(p.price for p in group)
            after = df.iloc[max(p.idx for p in group) + 1 :]
            tested = (after["high"] > lvl).any() if kind == "high" else (after["low"] < lvl).any()
            if not tested:
                out.append(Level(float(lvl), label, len(group), f"equal {kind}s ×{len(group)} (liquidity)"))
    return out


def prior_extremes(daily: pd.DataFrame | None, weekly: pd.DataFrame | None) -> list[Level]:
    out: list[Level] = []
    if daily is not None and len(daily) >= 1:
        d = daily.iloc[-1]
        out += [Level(float(d["high"]), "pdh", 2, "prior day high"), Level(float(d["low"]), "pdl", 2, "prior day low")]
    if weekly is not None and len(weekly) >= 1:
        w = weekly.iloc[-1]
        out += [Level(float(w["high"]), "pwh", 3, "prior week high"), Level(float(w["low"]), "pwl", 3, "prior week low")]
    return out


def detect_range(df: pd.DataFrame, atr_value: float, lookback: int = 48, max_width_atr: float = 8.0,
                 touch_tol_atr: float = 0.35, adx_max: float = 20.0) -> RangeInfo | None:
    """A sideways range: recent bars bounded within a band, ADX low, at least 2 touches per edge."""
    if len(df) < lookback or not np.isfinite(atr_value) or atr_value <= 0:
        return None
    w = df.iloc[-lookback:]
    hi, lo = float(w["high"].max()), float(w["low"].min())
    if (hi - lo) > max_width_atr * atr_value:
        return None
    if "adx" in w.columns:
        last_adx = w["adx"].iloc[-1]
        if pd.notna(last_adx) and float(last_adx) > adx_max:
            return None
    tol = touch_tol_atr * atr_value
    th = _touches(w["high"].to_numpy(dtype=float), hi, tol)
    tl = _touches(w["low"].to_numpy(dtype=float), lo, tol)
    if th < 2 or tl < 2:
        return None
    return RangeInfo(hi, lo, th, tl)


def _touches(arr: np.ndarray, level: float, tol: float) -> int:
    near = np.abs(arr - level) <= tol
    # count separate touch episodes, not consecutive bars
    return int(np.sum(near & ~np.concatenate(([False], near[:-1]))))


def fair_value_gaps(df: pd.DataFrame, lookback: int = 60) -> list[Level]:
    """Unfilled 3-candle imbalances in the last `lookback` bars (most recent first)."""
    out: list[Level] = []
    w = df.iloc[-lookback:]
    h = w["high"].to_numpy(dtype=float)
    lo = w["low"].to_numpy(dtype=float)
    for i in range(len(w) - 1, 1, -1):
        kind: Literal["bull", "bear"] | None = None
        if lo[i] > h[i - 2]:
            kind, top, bot = "bull", lo[i], h[i - 2]
        elif h[i] < lo[i - 2]:
            kind, top, bot = "bear", lo[i - 2], h[i]
        if kind is None:
            continue
        later_l = lo[i + 1 :]
        later_h = h[i + 1 :]
        filled = (later_l <= bot).any() if kind == "bull" else (later_h >= top).any()
        if not filled:
            out.append(Level(float((top + bot) / 2), "fvg", 1, f"{kind} FVG {bot:.6g}–{top:.6g}"))
        if len(out) >= 4:
            break
    return out


def nearest(levels: list[Level], price: float, side: Literal["below", "above"], min_dist: float = 0.0,
            max_dist: float = float("inf")) -> list[Level]:
    """Levels on one side of price within [min_dist, max_dist], nearest first."""
    if side == "below":
        cand = [lv for lv in levels if min_dist <= price - lv.price <= max_dist]
        return sorted(cand, key=lambda lv: price - lv.price)
    cand = [lv for lv in levels if min_dist <= lv.price - price <= max_dist]
    return sorted(cand, key=lambda lv: lv.price - price)
