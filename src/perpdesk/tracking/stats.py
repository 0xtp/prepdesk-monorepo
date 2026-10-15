"""Performance statistics in R, with honest uncertainty."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class RStats:
    n: int
    wins: int
    win_rate: float
    wilson_lo: float
    wilson_hi: float
    avg_r: float  # == expectancy in R per trade
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float | None
    max_dd_r: float
    max_losing_streak: int
    total_r: float
    breakeven_wr: float | None  # win rate needed to break even at the observed avg win / avg loss

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def r_stats(rs: Sequence[float]) -> RStats:
    a = np.asarray([x for x in rs if x is not None and math.isfinite(x)], dtype=float)
    n = int(a.size)
    if n == 0:
        return RStats(0, 0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, None, 0.0, 0, 0.0, None)
    wins = int((a > 0).sum())
    lo, hi = wilson(wins, n)
    pos, neg = a[a > 0], a[a <= 0]
    pf = float(pos.sum() / abs(neg.sum())) if neg.size and neg.sum() != 0 else (None if pos.size == 0 else math.inf)
    eq = np.cumsum(a)
    peak = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:]
    max_dd = float((peak - eq).max()) if n else 0.0
    streak = cur = 0
    for x in a:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    aw = float(pos.mean()) if pos.size else 0.0
    al = float(neg.mean()) if neg.size else 0.0
    be = abs(al) / (aw + abs(al)) if aw > 0 and al < 0 else None
    return RStats(n, wins, wins / n, lo, hi, float(a.mean()), aw, al, pf, max_dd, streak, float(a.sum()), be)


def breakeven_win_rate(rr: float) -> float:
    return 1 / (1 + rr) if rr > 0 else 1.0


def quarterly_expectancy(ts_ms: Sequence[int], rs: Sequence[float]) -> list[tuple[str, int, float]]:
    """[(quarter label, n, avg R)] in time order."""
    import pandas as pd

    if not len(rs):
        return []
    s = pd.Series(list(rs), index=pd.DatetimeIndex(pd.to_datetime(list(ts_ms), unit="ms", utc=True)).tz_localize(None))
    g = s.groupby(pd.DatetimeIndex(s.index).to_period("Q").astype(str))
    return [(str(k), int(v.size), float(v.mean())) for k, v in g]


def bootstrap_window_percentile(rs: Sequence[float], window: int = 30, q: float = 5.0, n_boot: int = 2000,
                                seed: int = 7) -> float | None:
    """q-th percentile of the mean R of `window` trades resampled from `rs`."""
    a = np.asarray(rs, dtype=float)
    if a.size < window:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(a, size=(n_boot, window), replace=True).mean(axis=1)
    return float(np.percentile(means, q))


def equity_curve(ts_ms: Sequence[int], rs: Sequence[float]) -> list[tuple[int, float]]:
    order = np.argsort(np.asarray(ts_ms))
    eq = 0.0
    out = []
    for i in order:
        eq += float(rs[i])
        out.append((int(ts_ms[i]), round(eq, 4)))
    return out
