"""Backtester: replays the live engine bar by bar with no lookahead.

Pass 1 (expensive, once per coin): at every trading-timeframe bar close T, build a MarketSnapshot
from data available at T only, run `analyse`, evaluate every setup parameter in the grid, and build
plans for each entry style. Indicator columns are precomputed on the full history; they are causal,
and higher-timeframe bars are only included once closed, which the no-lookahead test verifies.

Pass 2 (cheap): for each strategy (one setup at one parameter, or the composite bias), walk forward
taking one position at a time and score every plan on 5-minute candles with the live scorer.

Walk-forward: rolling 6-month train / 3-month test windows. For each test window the parameter with
the best train expectancy (pooled across coins) is used; only test-window trades count as
out-of-sample. The final 3 months are a hold-out evaluated once with parameters fit on all prior data.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from perpdesk.analysis.engine import STRUCTURE_WINDOW, EngineConfig, analyse
from perpdesk.analysis.factors import funding_zscore
from perpdesk.analysis.indicators import add_features, session_vwap
from perpdesk.analysis.setups import PARAM_GRIDS, SETUPS, SetupContext
from perpdesk.analysis.types import TF_MS, AssetMeta, MarketSnapshot, SetupSignal
from perpdesk.plan import Plan, PlanConfig, RiskInputs, build_plan
from perpdesk.timeutil import index_ms
from perpdesk.tracking.scorer import Outcome, PlanSpec, score
from perpdesk.verdict import room_check

LOOKBACK = {"1w": 260, "1d": 420, "4h": 700, "1h": 1500, "15m": 1000}
MONTH_MS = 30 * 86_400_000


@dataclass
class Frames:
    """Precomputed feature frames with close-time arrays for fast as-of slicing."""

    data: dict[str, pd.DataFrame]
    close_ms: dict[str, np.ndarray]

    @staticmethod
    def build(frames: dict[str, pd.DataFrame]) -> Frames:
        data, close = {}, {}
        for tf, df in frames.items():
            if df is None or df.empty:
                continue
            f = add_features(df)
            data[tf] = f
            span = 7 * 86_400_000 if tf == "1w" else TF_MS[tf]
            close[tf] = index_ms(f) + span
        return Frames(data, close)

    def asof(self, t_ms: int, lookback: dict[str, int] | None = None) -> dict[str, pd.DataFrame]:
        lb = lookback or LOOKBACK
        out = {}
        for tf, df in self.data.items():
            n = int(np.searchsorted(self.close_ms[tf], t_ms, side="right"))
            if n == 0:
                continue
            out[tf] = df.iloc[max(0, n - lb.get(tf, 500)) : n]
        return out


def _asof_series(df: pd.DataFrame | None, t: pd.Timestamp, delay: pd.Timedelta | None = None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    cutoff = t - delay if delay is not None else t
    out = df[df.index <= cutoff]
    return out if not out.empty else None


@dataclass
class BarRecord:
    t_ms: int
    direction: str
    confidence: int
    near_zero: bool
    flags: int
    grade_ex_gate: str
    signals: dict[str, list[str | None]]  # setup -> direction per grid param (None = no signal)
    plans: dict[str, Plan]  # "composite" | "limit" | "market"
    funding: float
    abs_score: float = 0.0
    room: dict[str, str] = field(default_factory=dict)  # plan key -> room-to-move status at this bar


@dataclass
class Trade:
    coin: str
    asset_class: str
    setup: str
    param_idx: int
    direction: str
    t_ms: int
    confidence: int
    grade_ex_gate: str
    rr_tp1: float
    filled: bool
    r: float | None
    tp1_before_sl: bool
    exit_reason: str
    end_ms: int
    period: str = "train"
    room: str = ""


@dataclass
class CoinRun:
    coin: str
    asset_class: str
    records: list[BarRecord]
    bars5: pd.DataFrame
    sources: dict[str, str]
    seconds: float
    first_ms: int
    last_ms: int
    trades: list[Trade] = field(default_factory=list)
    # optional 1-minute candle loader (start_ms, end_ms) used to re-score ambiguous 5-minute exits
    fine: Callable[[int, int], pd.DataFrame | None] | None = None
    fine_cache: dict[tuple[int, int], pd.DataFrame | None] = field(default_factory=dict)
    refined: int = 0  # ambiguous exits re-scored on 1m candles
    flipped: int = 0  # ...whose result changed


def pass1(coin: str, asset_class: str, meta: AssetMeta, frames: dict[str, pd.DataFrame],
          btc_frames: dict[str, pd.DataFrame] | None, funding: pd.DataFrame | None, fear_greed: pd.DataFrame | None,
          cme: pd.DataFrame | None, ecfg: EngineConfig, pcfg: PlanConfig, start_ms: int | None = None,
          stride: int = 1, progress: Callable[[str], None] | None = None) -> list[BarRecord]:
    from perpdesk.lab.room import LevelBook

    fr = Frames.build(frames)
    book = LevelBook(frames)
    bfr = Frames.build(btc_frames) if btc_frames else None
    tr_tf = ecfg.trading_tf
    closes = fr.close_ms[tr_tf]
    tr_df = fr.data[tr_tf]
    d1_close = fr.close_ms.get("1d")
    # warm-up: need EMA200 on the daily chart and enough trading bars
    warm_ms = int(d1_close[min(len(d1_close) - 1, 220)]) if d1_close is not None and len(d1_close) > 220 else int(closes[0])
    first = max(int(np.searchsorted(closes, warm_ms)), 300)
    if start_ms is not None:
        first = max(first, int(np.searchsorted(closes, start_ms)))
    risk = RiskInputs(250.0, 20.0)
    out: list[BarRecord] = []
    t_start = time.monotonic()
    for i in range(first, len(closes), stride):
        t_ms = int(closes[i])
        t = pd.Timestamp(t_ms, unit="ms", tz="UTC")
        c = fr.asof(t_ms)
        if tr_tf not in c or len(c[tr_tf]) < 60:
            continue
        fund = _asof_series(funding, t)
        cur_f = float(fund["rate"].iloc[-1]) if fund is not None else None
        prem = float(fund["premium"].iloc[-1]) if fund is not None and "premium" in fund else None
        snap = MarketSnapshot(
            coin=coin, asof=t, price=float(tr_df["close"].iloc[i]), meta=meta, candles=c,
            btc_candles=bfr.asof(t_ms) if bfr is not None else None,
            funding=fund.iloc[-800:] if fund is not None else None, current_funding=cur_f, current_premium=prem,
            fear_greed=_asof_series(fear_greed, t),
            # a Yahoo daily bar dated D is only complete after that session closes: use bars up to D-1
            cme_daily=_asof_series(cme, t, pd.Timedelta(days=1)) if coin in ("BTC", "ETH", "SOL") else None,
        )
        try:
            a = analyse(snap, ecfg)
        except ValueError:
            continue
        trading = c[tr_tf].iloc[-STRUCTURE_WINDOW:].copy()
        trading["vwap"] = session_vwap(trading)
        fz, _ = funding_zscore(snap.funding, cur_f)
        ctx = SetupContext(trading, c.get("4h"), c.get("1d"), fz, a.atr)
        signals: dict[str, list[str | None]] = {}
        for name, fn in SETUPS.items():
            sig_dirs: list[str | None] = []
            for p in PARAM_GRIDS[name]:
                sig = fn(ctx, p)
                sig_dirs.append(sig.direction if sig is not None else None)
            signals[name] = sig_dirs
        plans: dict[str, Plan] = {}
        for key, setup in (("composite", SetupSignal("composite", a.direction, "limit")),
                           ("limit", SetupSignal("trend_pullback", a.direction, "limit")),
                           ("market", SetupSignal("breakout", a.direction, "market"))):
            try:
                plans[key] = build_plan(replace(a, setup=setup), meta, risk, pcfg, None, cur_f or 0.0)
            except ValueError:
                continue
        if "market" in plans:  # same breakout setup, but never switched to a limit (entry variant study)
            try:
                plans["market_forced"] = build_plan(replace(a, setup=SetupSignal("breakout", a.direction, "market")),
                                                    meta, risk, replace(pcfg, limit_switch=False), None, cur_f or 0.0)
            except ValueError:
                pass
        if "composite" not in plans:
            continue
        g = plans["composite"].grade
        # same 1h-based levels the live card shows, whatever the trading timeframe (lab/room.py)
        lv = book.levels_at(t_ms, snap.price)
        room = {k: room_check(p.direction, p.entry, p.sl, p.tp1, lv).status for k, p in plans.items()}
        out.append(BarRecord(t_ms, a.direction, a.confidence, a.near_zero, len(a.flags), g, signals, plans, cur_f or 0.0,
                             abs(a.score), room))
        if progress and len(out) % 1000 == 0:
            progress(f"{coin}: {len(out)} bars analysed ({time.monotonic() - t_start:.0f}s)")
    return out


def _spec(p: Plan, t_ms: int, pcfg: PlanConfig, funding: float) -> PlanSpec:
    entry_fee = (pcfg.maker_fee_bps if p.entry_type == "limit" else pcfg.taker_fee_bps + pcfg.slippage_bps) / 1e4
    return PlanSpec(direction=p.direction, entry_type=p.entry_type, entry=p.entry, sl=p.sl, tp1=p.tp1, tp2=p.tp2,
                    runaway=p.runaway_price, created_ms=t_ms, expires_ms=t_ms + p.ttl_min * 60_000,
                    max_hold_ms=pcfg.max_hold_hours * 3_600_000, entry_fee=entry_fee, exit_fee=pcfg.taker_fee_bps / 1e4,
                    slippage=pcfg.slippage_bps / 1e4, funding_per_hour=funding)


def refine(run: CoinRun, spec: PlanSpec, coarse: pd.DataFrame, out: Outcome, end_ms: int) -> Outcome:
    """Re-score a trade whose exit candle touched both the stop and a target, using 1-minute candles up to the
    end of that candle (5-minute candles after it). Keeps the coarse result if 1-minute data is unavailable."""
    if run.fine is None or out.exit_ms is None:
        return out
    key = (spec.created_ms, out.exit_ms)
    if key not in run.fine_cache:
        run.fine_cache[key] = run.fine(spec.created_ms - 60_000, out.exit_ms + 300_000)
    f = run.fine_cache[key]
    if f is None or f.empty:
        return out
    cut = out.exit_ms + 300_000
    rest = coarse[index_ms(coarse) >= cut]
    f = f[index_ms(f) < cut]
    if f.empty:
        return out
    better = score(spec, pd.concat([f, rest]), end_ms)
    if better.exit_reason == "open":
        return out
    run.refined += 1
    if (better.exit_reason, round(better.r_multiple or 0, 6)) != (out.exit_reason, round(out.r_multiple or 0, 6)):
        run.flipped += 1
    return better


def simulate(run: CoinRun, setup: str, param_idx: int, pcfg: PlanConfig, one_at_a_time: bool = True) -> list[Trade]:
    """One position at a time. setup == 'composite' trades the composite plan on every flat bar.
    one_at_a_time=False scores every signal, even while an earlier one is still open (for portfolio studies, which
    apply their own position rules)."""
    bars = run.bars5
    t5 = index_ms(bars)
    horizon = pcfg.plan_ttl_min * 60_000 + pcfg.max_hold_hours * 3_600_000 + 600_000
    flat_until = -1
    trades: list[Trade] = []
    for rec in run.records:
        if one_at_a_time and rec.t_ms < flat_until:
            continue
        key = "composite"
        if setup == "composite":
            plan = rec.plans.get("composite")
        else:
            key = {"breakout": "market", "breakout_mkt": "market_forced"}.get(setup, "limit")
            d = rec.signals.get("breakout" if setup == "breakout_mkt" else setup, [None])[param_idx]
            if d is None or d != rec.direction:
                continue
            plan = rec.plans.get(key)
        if plan is None:
            continue
        lo = int(np.searchsorted(t5, rec.t_ms, side="left"))
        hi = int(np.searchsorted(t5, rec.t_ms + horizon, side="right"))
        if hi - lo < 2:
            break  # ran out of 5m data
        sl = bars.iloc[lo:hi]
        spec = _spec(plan, rec.t_ms, pcfg, rec.funding)
        out = score(spec, sl, int(t5[hi - 1]) + 300_000)
        if out.ambiguous and out.exit_ms is not None:
            out = refine(run, spec, sl, out, int(t5[hi - 1]) + 300_000)
        if out.exit_reason == "open":
            break  # unresolved at the end of the data
        end = out.exit_ms or rec.t_ms
        trades.append(Trade(run.coin, run.asset_class, setup, param_idx, rec.direction, rec.t_ms, rec.confidence,
                            rec.grade_ex_gate, plan.rr_tp1, out.filled, out.r_multiple, out.tp1_before_sl,
                            out.exit_reason, end, room=rec.room.get(key, "")))
        flat_until = end
    return trades


# ---------------------------------------------------------------- walk-forward


@dataclass
class WalkForward:
    windows: list[tuple[int, int, int]]  # (train_start, test_start, test_end)
    holdout_start: int
    chosen: dict[str, list[int]]  # setup -> chosen param idx per window
    final_params: dict[str, int]  # setup -> param idx fit on all pre-holdout data (used live)
    combos_tried: int


def walk_forward(runs: list[CoinRun], pcfg: PlanConfig, train_months: int = 6, test_months: int = 3,
                 holdout_months: int = 3, min_train_trades: int = 10) -> tuple[WalkForward, list[Trade]]:
    first = min(r.first_ms for r in runs)
    last = max(r.last_ms for r in runs)
    holdout_start = last - holdout_months * MONTH_MS
    windows = []
    ts = first + train_months * MONTH_MS
    while ts < holdout_start:
        te = min(ts + test_months * MONTH_MS, holdout_start)
        windows.append((ts - train_months * MONTH_MS, ts, te))
        ts = te
    all_trades: list[Trade] = []
    chosen: dict[str, list[int]] = {}
    final: dict[str, int] = {}
    combos = 0
    by_setup: dict[str, dict[int, list[Trade]]] = {}
    for run in runs:
        for setup in list(SETUPS) + ["composite"]:
            grid = range(len(PARAM_GRIDS[setup])) if setup != "composite" else range(1)
            for pi in grid:
                tr = simulate(run, setup, pi, pcfg)
                by_setup.setdefault(setup, {}).setdefault(pi, []).extend(tr)
    for setup, per_param in by_setup.items():
        n_params = len(per_param)
        chosen[setup] = []

        def best(lo: int, hi: int, per_param: dict[int, list[Trade]] = per_param) -> int:
            scores = {}
            for pi, trs in per_param.items():
                rs = [t.r for t in trs if lo <= t.t_ms < hi and t.filled and t.r is not None]
                if len(rs) >= min_train_trades:
                    scores[pi] = float(np.mean(rs))
            return max(scores, key=lambda k: scores[k]) if scores else 0

        for tr_start, te_start, te_end in windows:
            pi = best(tr_start, te_start) if n_params > 1 else 0
            combos += n_params
            chosen[setup].append(pi)
            for t in per_param[pi]:
                if te_start <= t.t_ms < te_end:
                    all_trades.append(replace(t, period="oos"))
        fp = best(first, holdout_start) if n_params > 1 else 0
        combos += n_params
        final[setup] = fp
        for t in per_param[fp]:
            if t.t_ms >= holdout_start:
                all_trades.append(replace(t, period="holdout"))
            elif windows and t.t_ms < windows[0][1]:
                all_trades.append(replace(t, period="train"))
    return WalkForward(windows, holdout_start, chosen, final, combos), all_trades


def ablation(coin: str, asset_class: str, meta: AssetMeta, frames: dict[str, pd.DataFrame],
             btc_frames: dict[str, pd.DataFrame] | None, funding: pd.DataFrame | None, fg: pd.DataFrame | None,
             cme: pd.DataFrame | None, ecfg: EngineConfig, pcfg: PlanConfig, bars5: pd.DataFrame,
             groups: Iterable[str], stride: int = 4) -> list[dict[str, Any]]:
    """Composite-strategy expectancy with each factor group switched off (subsampled every `stride` bars)."""
    out = []
    variants: list[tuple[str, frozenset[str]]] = [("all factors", frozenset())]
    variants += [(f"without {g}", frozenset({g})) for g in groups]
    for label, disabled in variants:
        cfg = replace(ecfg, disabled_groups=disabled)
        recs = pass1(coin, asset_class, meta, frames, btc_frames, funding, fg, cme, cfg, pcfg, stride=stride)
        run = CoinRun(coin, asset_class, recs, bars5, {}, 0, recs[0].t_ms if recs else 0, recs[-1].t_ms if recs else 0)
        trs = [t for t in simulate(run, "composite", 0, pcfg) if t.filled and t.r is not None]
        rs = [t.r for t in trs if t.r is not None]
        out.append({"variant": label, "n": len(rs), "avg_r": float(np.mean(rs)) if rs else math.nan,
                    "win": float(np.mean([r > 0 for r in rs])) if rs else math.nan})
    return out


def score_quantiles(runs: list[CoinRun]) -> tuple[float, ...]:
    """|score| at percentiles 0..100 pooled over every analysed bar of every coin."""
    vals = np.asarray([r.abs_score for run in runs for r in run.records], dtype=float)
    if vals.size < 100:
        return ()
    return tuple(round(float(x), 6) for x in np.percentile(vals, np.arange(101)))


def apply_confidence(runs: list[CoinRun], quantiles: tuple[float, ...], min_confidence: int) -> None:
    """Re-express every record's confidence as a percentile of historical signal strength and
    re-derive its grade (B >= MIN_CONFIDENCE, else C; low R:R, liquidity or near-zero -> C)."""
    from perpdesk.analysis.engine import percentile_confidence

    if not quantiles:
        return
    for run in runs:
        for r in run.records:
            r.confidence = percentile_confidence(r.abs_score, quantiles)
            comp = r.plans.get("composite")
            capped = r.near_zero or (comp is not None and any(f.code in ("low_rr", "liquidity") for f in comp.flags))
            r.grade_ex_gate = "C" if capped or r.confidence < min_confidence else "B"
