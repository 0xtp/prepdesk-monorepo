"""Score a plan against what price actually did. Pure function used by live tracking and backtests.

Conservative rules (decided with the user):
  * if the stop and a target are both inside one candle, the stop is assumed to hit first;
  * a limit entry fills only if price trades through the entry price; a market entry fills at the
    next candle's open;
  * an unfilled plan expires at its TTL or when price runs 1R away from entry ("not triggered");
  * after TP1 (50%), the stop moves to breakeven; the remainder exits at TP2, breakeven, or at the
    candle close when MAX_HOLD_HOURS elapses;
  * exits are charged taker fees; the stop also pays slippage.
R is measured against the full planned loss incl. costs, so a clean stop-out is exactly -1R.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from perpdesk.timeutil import index_ms


@dataclass(frozen=True)
class PlanSpec:
    direction: str  # LONG|SHORT
    entry_type: str  # limit|market
    entry: float
    sl: float
    tp1: float
    tp2: float
    runaway: float
    created_ms: int
    expires_ms: int
    max_hold_ms: int
    entry_fee: float  # rate
    exit_fee: float  # rate
    slippage: float  # rate on stop exits
    funding_per_hour: float = 0.0  # positive = longs pay


@dataclass(frozen=True)
class Outcome:
    filled: bool
    fill_ms: int | None
    fill_price: float | None
    tp1_hit: bool
    exit_reason: str  # sl|tp2|be|timeout|not_triggered|open
    exit_ms: int | None
    exit_price: float | None
    r_multiple: float | None
    mfe_r: float | None
    mae_r: float | None
    # the exit candle also touched a target: with coarse candles the true order is unknown and the stop was
    # assumed first. Backtests re-score such trades on 1-minute candles.
    ambiguous: bool = False

    @property
    def resolved(self) -> bool:
        return self.exit_reason != "open"

    @property
    def tp1_before_sl(self) -> bool:
        return self.tp1_hit


def _loss_per_coin(p: PlanSpec, fill: float, d: int, hours: float) -> float:
    funding = max(0.0, d * p.funding_per_hour * hours) * fill
    return abs(fill - p.sl) + fill * p.entry_fee + p.sl * (p.exit_fee + p.slippage) + funding


def score(p: PlanSpec, bars: pd.DataFrame, now_ms: int) -> Outcome:
    """bars: closed candles (open-time UTC index, columns open/high/low/close) covering created_ms..now."""
    d = 1 if p.direction == "LONG" else -1
    if bars.empty:
        return _pending(p, now_ms)
    ts = index_ms(bars)
    sel = ts >= p.created_ms
    ts = ts[sel]
    o = bars["open"].to_numpy(dtype=float)[sel]
    h = bars["high"].to_numpy(dtype=float)[sel]
    lo = bars["low"].to_numpy(dtype=float)[sel]
    c = bars["close"].to_numpy(dtype=float)[sel]
    step = int(np.median(np.diff(ts))) if len(ts) > 1 else 60_000

    fill_i: int | None = None
    fill_px = 0.0
    for i in range(len(ts)):
        if ts[i] >= p.expires_ms:
            return Outcome(False, None, None, False, "not_triggered", p.expires_ms, None, None, None, None)
        if p.entry_type == "market":
            fill_i, fill_px = i, o[i]
            break
        # cancel if price runs away 1R before the limit is touched
        touched = lo[i] <= p.entry if d > 0 else h[i] >= p.entry
        ran = (h[i] >= p.runaway) if d > 0 else (lo[i] <= p.runaway)
        if touched:
            fill_i, fill_px = i, p.entry
            break
        if ran:
            return Outcome(False, None, None, False, "not_triggered", int(ts[i]), None, None, None, None)
    if fill_i is None:
        return _pending(p, now_ms)

    risk_gross = abs(fill_px - p.sl)
    if risk_gross <= 0:
        return Outcome(True, int(ts[fill_i]), fill_px, False, "sl", int(ts[fill_i]), p.sl, -1.0, 0.0, -1.0)
    deadline = int(ts[fill_i]) + p.max_hold_ms
    tp1_hit = False
    stop = p.sl
    mfe = mae = 0.0
    realised = 0.0  # per coin, net, of the half closed at TP1
    for i in range(fill_i, len(ts)):
        hi_, lo_ = h[i], lo[i]
        fav = (hi_ - fill_px) * d if d > 0 else (fill_px - lo_)
        adv = (lo_ - fill_px) * d if d > 0 else (fill_px - hi_)
        mfe = max(mfe, fav / risk_gross)
        mae = min(mae, adv / risk_gross)
        hit_stop = lo_ <= stop if d > 0 else hi_ >= stop
        hit_tp1 = (hi_ >= p.tp1 if d > 0 else lo_ <= p.tp1) and not tp1_hit
        hit_tp2 = hi_ >= p.tp2 if d > 0 else lo_ <= p.tp2
        touched_target = hit_tp1 or hit_tp2
        if i == fill_i and p.entry_type == "limit":
            # in the fill candle we cannot know the order of events: only the stop counts
            hit_tp1 = hit_tp2 = False
        hours = (int(ts[i]) + step - int(ts[fill_i])) / 3_600_000
        loss = _loss_per_coin(p, fill_px, d, hours)
        if hit_stop:
            if not tp1_hit:
                return Outcome(True, int(ts[fill_i]), fill_px, False, "sl", int(ts[i]), stop, -1.0, mfe, mae,
                               ambiguous=touched_target)
            leg = _leg(p, fill_px, stop, d, hours, is_stop=True)
            r = (realised + 0.5 * leg) / loss
            return Outcome(True, int(ts[fill_i]), fill_px, True, "be", int(ts[i]), stop, r, mfe, mae,
                           ambiguous=touched_target)
        if hit_tp1:
            tp1_hit = True
            realised = 0.5 * _leg(p, fill_px, p.tp1, d, hours, is_stop=False)
            stop = fill_px  # breakeven
            if hit_tp2:
                r = (realised + 0.5 * _leg(p, fill_px, p.tp2, d, hours, is_stop=False)) / loss
                return Outcome(True, int(ts[fill_i]), fill_px, True, "tp2", int(ts[i]), p.tp2, r, mfe, mae)
            continue
        if tp1_hit and hit_tp2:
            r = (realised + 0.5 * _leg(p, fill_px, p.tp2, d, hours, is_stop=False)) / loss
            return Outcome(True, int(ts[fill_i]), fill_px, True, "tp2", int(ts[i]), p.tp2, r, mfe, mae)
        if int(ts[i]) + step >= deadline:
            share = 0.5 if tp1_hit else 1.0
            r = (realised + share * _leg(p, fill_px, c[i], d, hours, is_stop=False)) / loss
            return Outcome(True, int(ts[fill_i]), fill_px, tp1_hit, "timeout", int(ts[i]) + step, float(c[i]), r, mfe, mae)
    return Outcome(True, int(ts[fill_i]), fill_px, tp1_hit, "open", None, None, None, mfe, mae)


def _leg(p: PlanSpec, fill: float, exit_px: float, d: int, hours: float, is_stop: bool) -> float:
    """Net P&L per coin for closing at exit_px (fees on both sides, slippage on stops, funding paid)."""
    gross = (exit_px - fill) * d
    fees = fill * p.entry_fee + exit_px * (p.exit_fee + (p.slippage if is_stop else 0.0))
    funding = d * p.funding_per_hour * hours * fill
    return gross - fees - funding


def _pending(p: PlanSpec, now_ms: int) -> Outcome:
    if now_ms >= p.expires_ms:
        return Outcome(False, None, None, False, "not_triggered", p.expires_ms, None, None, None, None)
    return Outcome(False, None, None, False, "open", None, None, None, None, None)
