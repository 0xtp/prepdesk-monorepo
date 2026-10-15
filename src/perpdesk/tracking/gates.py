"""Statistical gates (backtest) and the live gate. A setup earns grade A only by passing all of them."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from perpdesk.tracking.stats import bootstrap_window_percentile, quarterly_expectancy, r_stats


@dataclass(frozen=True)
class GateConfig:
    min_trades: int = 100
    min_expectancy_r: float = 0.15
    min_pf: float = 1.3
    max_dd_r: float = 15.0
    min_positive_quarters: int = 3
    quarters_considered: int = 4


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)  # failures (empty if passed) plus informational notes
    stats: dict[str, object] = field(default_factory=dict)


def evaluate(ts_ms: Sequence[int], rs: Sequence[float], rr: Sequence[float], won_tp1: Sequence[bool],
             cfg: GateConfig) -> GateResult:
    """rs: OOS R multiples (net of costs); rr: planned net R:R to TP1; won_tp1: TP1 hit before SL."""
    s = r_stats(rs)
    n = s.n
    fails: list[str] = []
    avg_rr = float(np.mean(rr)) if len(rr) else 0.0
    wins_tp1 = int(sum(bool(x) for x in won_tp1))
    from perpdesk.tracking.stats import wilson

    tp1_lo, tp1_hi = wilson(wins_tp1, n)
    # break-even win rate for the realised payoff profile (wins vs losses in R)
    be = s.breakeven_wr if s.breakeven_wr is not None else (1 / (1 + avg_rr) if avg_rr > 0 else 1.0)
    q = quarterly_expectancy(ts_ms, rs)[-cfg.quarters_considered:]
    pos_q = sum(1 for _, cnt, avg in q if cnt > 0 and avg > 0)
    if n < cfg.min_trades:
        fails.append(f"only {n} OOS trades (needs {cfg.min_trades})")
    if s.avg_r <= cfg.min_expectancy_r:
        fails.append(f"expectancy {s.avg_r:+.2f}R ≤ {cfg.min_expectancy_r:+.2f}R")
    if s.wilson_lo <= be:
        fails.append(f"win-rate lower bound {s.wilson_lo:.0%} ≤ break-even {be:.0%}")
    pf = s.profit_factor
    if pf is None or (math.isfinite(pf) and pf <= cfg.min_pf):
        fails.append(f"profit factor {pf if pf is not None else 0:.2f} ≤ {cfg.min_pf:g}")
    if s.max_dd_r >= cfg.max_dd_r:
        fails.append(f"max drawdown {s.max_dd_r:.1f}R ≥ {cfg.max_dd_r:g}R")
    if len(q) < cfg.quarters_considered or pos_q < cfg.min_positive_quarters:
        fails.append(f"positive in {pos_q} of {len(q)} OOS quarters (needs {cfg.min_positive_quarters} of "
                     f"{cfg.quarters_considered})")
    stats: dict[str, object] = dict(s.as_dict())
    stats.update({"avg_rr": avg_rr, "be_wr": be, "tp1_rate": wins_tp1 / n if n else 0.0, "tp1_lo": tp1_lo,
                  "tp1_hi": tp1_hi, "quarters": q, "positive_quarters": pos_q})
    return GateResult(not fails, fails, stats)


@dataclass
class LiveCheck:
    demote: bool
    reason: str | None
    n: int
    expectancy: float | None
    rolling30: float | None
    p5: float | None


def live_check(live_rs: Sequence[float], oos_rs: Sequence[float], min_sample: int) -> LiveCheck:
    n = len(live_rs)
    if n < min_sample:
        return LiveCheck(False, None, n, None, None, None)
    exp = float(np.mean(live_rs))
    roll = float(np.mean(live_rs[-30:])) if n >= 30 else None
    p5 = bootstrap_window_percentile(oos_rs, 30, 5.0)
    if exp < 0:
        return LiveCheck(True, f"live expectancy {exp:+.2f}R over {n} signals", n, exp, roll, p5)
    if roll is not None and p5 is not None and roll < p5:
        return LiveCheck(True, f"last 30 live trades averaged {roll:+.2f}R, below the backtest 5th percentile "
                               f"({p5:+.2f}R)", n, exp, roll, p5)
    return LiveCheck(False, None, n, exp, roll, p5)
