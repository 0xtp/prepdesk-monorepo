"""Simulate one lab trade on fine candles (5m in backtests, 1m-15m live). Conservative like the main scorer:
market fill at the first candle open after the signal; a stop inside a candle counts before anything else."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from perpdesk.lab.strategies import CarryConfig, Entry, carry_cost, carry_rolling, carry_signal
from perpdesk.timeutil import index_ms
from perpdesk.tracking.scorer import Outcome, PlanSpec, score


@dataclass(frozen=True)
class Fees:
    entry: float = 0.00045 + 0.0003  # taker + slippage (market entry)
    exit: float = 0.00045
    slippage: float = 0.0003  # extra on stop exits


def fixed_spec(e: Entry, signal_close: float, t_ms: int, f: Fees, funding_per_hour: float = 0.0) -> PlanSpec:
    away = abs(signal_close - e.stop) * 10  # market entry: runaway/expiry do not apply
    d = 1 if e.direction == "LONG" else -1
    return PlanSpec(direction=e.direction, entry_type="market", entry=signal_close, sl=e.stop, tp1=e.tp1, tp2=e.tp2,
                    runaway=signal_close + d * away, created_ms=t_ms, expires_ms=t_ms + 3_600_000,
                    max_hold_ms=e.max_hold_h * 3_600_000, entry_fee=f.entry, exit_fee=f.exit, slippage=f.slippage,
                    funding_per_hour=funding_per_hour)


@dataclass(frozen=True)
class TrailResult:
    filled: bool
    fill_ms: int | None
    fill_px: float | None
    exit_reason: str  # trail|stop|timeout|open
    exit_ms: int | None
    exit_px: float | None
    r: float | None
    stop_now: float | None
    best: float | None


def simulate_trail(e: Entry, t_ms: int, bars: pd.DataFrame, now_ms: int, f: Fees,
                   funding_per_hour: float = 0.0) -> TrailResult:
    """Chandelier exit: stop = max(stop, best price - mult × ATR) for longs (mirror for shorts). The stop used
    inside a candle is the one known before that candle; the candle's extreme only moves it for the next one."""
    d = 1 if e.direction == "LONG" else -1
    if bars.empty:
        return TrailResult(False, None, None, "open", None, None, None, e.stop, None)
    ts = index_ms(bars)
    sel = ts >= t_ms
    ts = ts[sel]
    if len(ts) == 0:
        return TrailResult(False, None, None, "open", None, None, None, e.stop, None)
    o = bars["open"].to_numpy(float)[sel]
    h = bars["high"].to_numpy(float)[sel]
    lo = bars["low"].to_numpy(float)[sel]
    c = bars["close"].to_numpy(float)[sel]
    step = int(np.median(np.diff(ts))) if len(ts) > 1 else 60_000
    fill = float(o[0])
    fill_ms = int(ts[0])
    if (fill - e.stop) * d <= 0:  # gapped through the stop before the fill
        return TrailResult(True, fill_ms, fill, "stop", fill_ms, fill, -1.0, e.stop, fill)
    loss0 = abs(fill - e.stop) + fill * f.entry + e.stop * (f.exit + f.slippage)
    stop, best = e.stop, fill
    deadline = fill_ms + e.max_hold_h * 3_600_000

    def r_of(px: float, end_ms: int, is_stop: bool) -> float:
        hours = (end_ms - fill_ms) / 3_600_000
        net = (px - fill) * d - fill * f.entry - px * (f.exit + (f.slippage if is_stop else 0.0))
        net -= d * funding_per_hour * hours * fill
        return net / loss0

    for i in range(len(ts)):
        hit = lo[i] <= stop if d > 0 else h[i] >= stop
        if hit:
            px = min(o[i], stop) if d > 0 else max(o[i], stop)  # a gap through the stop fills at the open
            reason = "trail" if (stop - e.stop) * d > 0 else "stop"
            return TrailResult(True, fill_ms, fill, reason, int(ts[i]), px, r_of(px, int(ts[i]) + step, True), stop,
                               best)
        best = max(best, h[i]) if d > 0 else min(best, lo[i])
        cand = best - d * e.trail_mult * e.atr
        stop = max(stop, cand) if d > 0 else min(stop, cand)
        if int(ts[i]) + step >= deadline:
            return TrailResult(True, fill_ms, fill, "timeout", int(ts[i]) + step, float(c[i]),
                               r_of(float(c[i]), int(ts[i]) + step, False), stop, best)
    return TrailResult(True, fill_ms, fill, "open", None, None, None, stop, best)


def simulate_fixed(e: Entry, signal_close: float, t_ms: int, bars: pd.DataFrame, now_ms: int, f: Fees,
                   funding_per_hour: float = 0.0) -> Outcome:
    return score(fixed_spec(e, signal_close, t_ms, f, funding_per_hour), bars, now_ms)


# ---------------------------------------------------------------- funding carry

@dataclass(frozen=True)
class CarryTrade:
    start_ms: int
    end_ms: int | None  # None = still open
    hours: int
    funding: float  # fraction of notional received
    cost: float  # fraction of notional paid in fees/slippage
    net: float  # funding - cost

    @property
    def return_on_capital(self) -> float:
        return self.net / CarryConfig().capital_per_notional


def simulate_carry(rates: pd.Series, c: CarryConfig | None = None) -> list[CarryTrade]:
    """Hourly funding series (rate per hour, positive = longs pay). Short perp + long spot is held while the
    24h average says so; each hour held earns that hour's rate (negative hours cost money)."""
    c = c or CarryConfig()
    if rates.empty:
        return []
    avg = carry_rolling(rates).to_numpy(float)
    r = rates.to_numpy(float)
    ts = index_ms(rates.to_frame())
    trades: list[CarryTrade] = []
    holding, start, got, n = False, 0, 0.0, 0
    for i in range(len(r)):
        if holding:
            got += r[i]
            n += 1
        want = carry_signal(avg[i], holding, c)
        if want and not holding:
            holding, start, got, n = True, int(ts[i]), 0.0, 0
        elif holding and not want:
            cost = carry_cost(c)
            trades.append(CarryTrade(start, int(ts[i]), n, got, cost, got - cost))
            holding = False
    if holding:
        cost = carry_cost(c)
        trades.append(CarryTrade(start, None, n, got, cost, got - cost))
    return trades
