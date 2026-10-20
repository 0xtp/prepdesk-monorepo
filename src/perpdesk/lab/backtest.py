"""Lab backtest: every lab strategy on every coin, one position per strategy per coin at a time.
Fixed settings, no optimisation, so there is no in-sample/out-of-sample split to game: every year counts."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from perpdesk.analysis.types import TF_MS
from perpdesk.lab.room import LevelBook
from perpdesk.lab.sim import Fees, simulate_carry, simulate_fixed, simulate_trail
from perpdesk.lab.strategies import H4_MS, STRATEGIES, CarryConfig, Strategy
from perpdesk.timeutil import index_ms
from perpdesk.tracking.stats import r_stats

BASE_FUNDING = 0.0000125  # Hyperliquid's default hourly rate when no history exists (0.01% per 8h)


@dataclass
class LabTrade:
    strategy: str
    coin: str
    direction: str
    signal_ms: int
    exit_ms: int
    exit_reason: str
    r: float
    year: int
    room: str = ""  # ok | caution | block | n/a: room-to-move check at the signal (verdict.room_check)


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def _funding_at(funding: pd.DataFrame, t_ms: int) -> float:
    if funding is None or funding.empty:
        return BASE_FUNDING
    ms = index_ms(funding)
    i = int(np.searchsorted(ms, t_ms, side="right")) - 1
    if i < 0:
        return BASE_FUNDING
    lo = max(0, i - 23)
    return float(funding["rate"].iloc[lo:i + 1].mean())


def run_strategy(strat: Strategy, coin: str, c4h: pd.DataFrame, d1: pd.DataFrame, btc_d1: pd.DataFrame | None,
                 c5m: pd.DataFrame, funding: pd.DataFrame, start_ms: int, fine: Any = None,
                 book: Any = None, bar_ms: int = H4_MS) -> list[LabTrade]:
    """bars: the strategy's candles (4h normally; 1h to test a faster version of the same rules)."""
    from perpdesk.backtest.runner import CoinRun, refine

    feat = strat.prepare(c4h, d1, btc_d1, bar_ms)
    t5 = index_ms(c5m)
    if len(t5) == 0:
        return []
    fees = Fees()
    out: list[LabTrade] = []
    flat_until = -1
    run = CoinRun(coin, "", [], c5m, {}, 0.0, 0, 0, fine=fine)
    close_ms = index_ms(feat) + bar_ms
    for k, row in enumerate(feat.itertuples(index=False)):
        t = int(close_ms[k])
        if t < start_ms or t < flat_until:
            continue
        try:
            e = strat.entry_at(row)
        except (TypeError, ValueError):
            continue
        if e is None or any(np.isnan(x) for x in (e.stop, e.tp1, e.tp2)):
            continue
        lo = int(np.searchsorted(t5, t, side="left"))
        hi = int(np.searchsorted(t5, t + (e.max_hold_h + 2) * 3_600_000, side="right"))
        if hi - lo < 2:
            break
        bars = c5m.iloc[lo:hi]
        end = int(t5[hi - 1]) + 300_000
        fr = _funding_at(funding, t)
        if e.exit == "trail":
            tr = simulate_trail(e, t, bars, end, fees, fr)
            if tr.exit_reason == "open" or tr.r is None or tr.exit_ms is None:
                break
            r_val, reason, exit_ms = tr.r, tr.exit_reason, tr.exit_ms
        else:
            spec_close = float(getattr(row, "close"))  # noqa: B009
            oc = simulate_fixed(e, spec_close, t, bars, end, fees, fr)
            if oc.ambiguous and oc.exit_ms is not None and fine is not None:
                from perpdesk.lab.sim import fixed_spec

                oc = refine(run, fixed_spec(e, spec_close, t, fees, fr), bars, oc, end)
            if oc.exit_reason == "open" or oc.r_multiple is None or oc.exit_ms is None:
                if oc.exit_reason == "not_triggered":
                    continue
                break
            r_val, reason, exit_ms = oc.r_multiple, oc.exit_reason, oc.exit_ms
        room = book.room(t, e.direction, float(getattr(row, "close")), e.stop, e.tp1).status if book else ""  # noqa: B009
        out.append(LabTrade(strat.name, coin, e.direction, t, exit_ms, reason, float(r_val),
                            datetime.fromtimestamp(t / 1000, UTC).year, room))
        flat_until = exit_ms
    return out


def summarize(trades: list[LabTrade], risk_pct: float = 0.5) -> dict[str, Any]:
    def block(rs: list[float]) -> dict[str, Any]:
        if not rs:
            return {"n": 0}
        st = r_stats(rs).as_dict()
        return {"n": st["n"], "win_rate": st["win_rate"], "avg_r": st["avg_r"], "sum_r": float(np.sum(rs)),
                "pf": st.get("profit_factor"), "max_dd_r": st["max_dd_r"],
                "pct_at_risk": float(np.sum(rs)) * risk_pct}

    by: dict[str, Any] = {}
    for name in sorted({t.strategy for t in trades}):
        ts = sorted((t for t in trades if t.strategy == name), key=lambda t: t.exit_ms)
        years = sorted({t.year for t in ts})
        by[name] = {
            "all": block([t.r for t in ts]),
            "direction": {d: block([t.r for t in ts if t.direction == d]) for d in ("LONG", "SHORT")},
            "year": {str(y): block([t.r for t in ts if t.year == y]) for y in years},
            "year_dir": {f"{y} {d}": block([t.r for t in ts if t.year == y and t.direction == d])
                         for y in years for d in ("LONG", "SHORT")},
            "coin": {c: block([t.r for t in ts if t.coin == c]) for c in sorted({t.coin for t in ts})},
            "coin_year": {c: {str(y): float(np.sum([t.r for t in ts if t.coin == c and t.year == y]))
                              for y in years if any(t.coin == c and t.year == y for t in ts)}
                          for c in sorted({t.coin for t in ts})},
            "exits": {k: sum(1 for t in ts if t.exit_reason == k) for k in sorted({t.exit_reason for t in ts})},
            "room": {k: block([t.r for t in ts if t.room == k]) for k in ("ok", "caution", "block", "n/a")},
            "room_filtered": block([t.r for t in ts if t.room != "block"]),
            "room_year": {f"{y} {k}": block([t.r for t in ts if t.year == y and t.room == k])
                          for y in years for k in ("ok", "caution", "block")},
        }
    return by


def summarize_carry(per_coin: dict[str, list[Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    span_years = max((end_ms - start_ms) / (365.25 * 86_400_000), 1e-9)
    for coin, trades in per_coin.items():
        if not trades:
            out[coin] = {"n": 0}
            continue
        net = sum(t.net for t in trades)
        cap = CarryConfig().capital_per_notional
        hours = sum(t.hours for t in trades)
        out[coin] = {"n": len(trades), "hours_held": hours, "net_pct_notional": net * 100,
                     "apr_on_capital_pct": net / cap / span_years * 100,
                     "apr_while_held_pct": (net / cap) / max(hours / 8766, 1e-9) * 100,
                     "worst_trade_pct": min(t.net for t in trades) * 100}
    return out


def run_lab(coins: list[str], start: date, end: date, cache_dir: Path, out_dir: Path, tf: str = "4h"
            ) -> dict[str, Any]:
    from perpdesk.backtest.history import load_history
    from perpdesk.backtest.main import minute_loader

    t0 = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    warm = start - timedelta(days=150)
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
    btc = load_history("BTC", warm, end, cache_dir)
    btc_d1 = btc.frames()["1d"]
    trades: list[LabTrade] = []
    carry: dict[str, list[Any]] = {}
    sources: dict[str, Any] = {}
    last_ms = start_ms
    for coin in coins:
        hist = btc if coin == "BTC" else load_history(coin, warm, end, cache_dir)
        if hist.c1h.empty or hist.c5m.empty:
            _log(f"{coin}: no history, skipped")
            continue
        fr = hist.frames()
        sources[coin] = {**hist.sources, "first_1h": f"{hist.c1h.index[0]:%Y-%m-%d}"}
        last_ms = max(last_ms, int(index_ms(hist.c5m)[-1]))
        fine = minute_loader(coin, cache_dir)
        book = LevelBook(fr)
        for strat in STRATEGIES.values():
            ts = time.monotonic()
            got = run_strategy(strat, coin, fr[tf], fr["1d"], btc_d1, hist.c5m, hist.funding, start_ms, fine, book,
                               TF_MS[tf])
            trades.extend(got)
            avg = float(np.mean([t.r for t in got])) if got else 0.0
            _log(f"{coin:5s} {strat.name:16s} n={len(got):4d} avgR={avg:+.3f} ({time.monotonic() - ts:.0f}s)")
        rates = hist.funding["rate"] if not hist.funding.empty else pd.Series(dtype=float)
        rates = rates[index_ms(rates.to_frame()) >= start_ms] if not rates.empty else rates
        carry[coin] = simulate_carry(rates)
        _log(f"{coin:5s} funding_carry    n={len(carry[coin]):4d} (funding history from "
             f"{rates.index[0]:%Y-%m-%d})" if not rates.empty else f"{coin}: no funding history")
    summary = {"start": start.isoformat(), "end": end.isoformat(), "tf": tf, "coins": coins, "sources": sources,
               "strategies": summarize(trades), "carry": summarize_carry(carry, start_ms, last_ms),
               "seconds": round(time.monotonic() - t0)}
    (out_dir / "lab_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "lab_trades.json").write_text(json.dumps([asdict(t) for t in trades]))
    for line in report_lines(summary):
        _log(line)
    return summary


def report_lines(s: dict[str, Any]) -> list[str]:
    def fmt(b: dict[str, Any]) -> str:
        if not b.get("n"):
            return "n=   0"
        pf = b.get("pf")
        return (f"n={b['n']:4d} win={b['win_rate']:4.0%} avgR={b['avg_r']:+.3f} sumR={b['sum_r']:+7.1f} "
                f"PF={min(pf or 0, 99):5.2f} maxDD={b['max_dd_r']:5.1f}R  @0.5%risk={b['pct_at_risk']:+6.1f}%")

    out = [f"LAB RESULTS {s.get('tf', '4h')} {s['start']} -> {s['end']} ({s['seconds']}s)"]
    for name, b in s["strategies"].items():
        out.append(f"== {name}: {fmt(b['all'])}")
        for d, x in b["direction"].items():
            out.append(f"   {d:5s}        {fmt(x)}")
        for y, x in b["year_dir"].items():
            if x.get("n"):
                out.append(f"   {y:10s}  {fmt(x)}")
        for c, x in b["coin"].items():
            yrs = b.get("coin_year", {}).get(c, {})
            pos = sum(1 for v in yrs.values() if v > 0)
            out.append(f"   coin {c:6s}  {fmt(x)}  years+ {pos}/{len(yrs)}: "
                       + " ".join(f"{y[2:]}:{v:+.1f}" for y, v in yrs.items()))
        out.append(f"   exits {b['exits']}")
        out.append("   room to move at the signal (block = strong level within 0.5R in the way; caution = before TP1):")
        for k, x in b.get("room", {}).items():
            if x.get("n"):
                out.append(f"   room {k:8s}  {fmt(x)}")
        if b.get("room_filtered"):
            out.append(f"   if 'block' were skipped: {fmt(b['room_filtered'])}")
        for y, x in b.get("room_year", {}).items():
            if x.get("n"):
                out.append(f"   {y:13s}  {fmt(x)}")
    out.append("== funding_carry (short perp + long spot), per coin:")
    for c, x in s["carry"].items():
        if x.get("n"):
            out.append(f"   {c:6s} trades={x['n']:3d} held={x['hours_held']:6d}h net={x['net_pct_notional']:+7.2f}% of "
                       f"notional  APR on capital (whole period)={x['apr_on_capital_pct']:+6.2f}%  "
                       f"while held={x['apr_while_held_pct']:+6.2f}%  worst={x['worst_trade_pct']:+.2f}%")
        else:
            out.append(f"   {c:6s} no carry trades")
    return out
