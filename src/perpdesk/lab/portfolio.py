"""Portfolio study: the breakout and trend-follower trade histories (2021 onward) replayed as ONE account with the
position rules the live bots use, then with each proposed fix. Every trade risks the same 1R.

Inputs: every signal of each bot scored on its own (backtest --dump-breakout, lab --all-signals), so the rules here
decide which ones would really have been taken. Regime features (4h ADX) use only candles closed before the signal.

The variants and the pass rule are fixed in VARIANTS / passes() BEFORE looking at results (docs/PHASE2.md,
"Portfolio study"), so the study can't be tuned to the answer."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

H4 = 4 * 3_600_000
DAY = 86_400_000


@dataclass
class T:
    bot: str
    coin: str
    direction: str
    t: int  # signal (4h close) time
    end: int  # exit time
    r: float
    adx: float = float("nan")  # coin 4h ADX14 at the signal
    btc_adx: float = float("nan")


@dataclass
class Rules:
    caps: dict[str, int] = field(default_factory=lambda: {"breakout": 3, "trend_follow": 6})
    max_same_dir: int | None = None  # both bots together
    max_open_r: float | None = None
    no_coin_overlap: bool = False  # a coin held by one bot can't be taken by the other
    min_adx: float | None = None
    min_btc_adx: float | None = None
    cooloff: tuple[int, int] | None = None  # (n losses in a row in a direction, pause hours)


VARIANTS: dict[str, Rules] = {
    "BASE (live rules today)": Rules(),
    "F1a max 2 same direction + no coin twice": Rules(max_same_dir=2, no_coin_overlap=True),
    "F1b max 1 same direction + no coin twice": Rules(max_same_dir=1, no_coin_overlap=True),
    "F1c max 3R open in total + no coin twice": Rules(max_open_r=3, no_coin_overlap=True),
    "F3a skip when coin 4h ADX < 20": Rules(min_adx=20),
    "F3b skip when BTC 4h ADX < 20": Rules(min_btc_adx=20),
    "F3c pause a direction 48h after 3 losses in a row": Rules(cooloff=(3, 48)),
}


def load_trades(breakout_json: Path | None, lab_json: Path | None) -> list[T]:
    out: list[T] = []
    if breakout_json and breakout_json.exists():
        for x in json.loads(breakout_json.read_text()):
            if x.get("filled") and x.get("r") is not None:
                out.append(T("breakout", x["coin"], x["direction"], int(x["t_ms"]), int(x["end_ms"]), float(x["r"])))
    if lab_json and lab_json.exists():
        for x in json.loads(lab_json.read_text()):
            if x["strategy"] == "trend_follow":
                out.append(T("trend_follow", x["coin"], x["direction"], int(x["signal_ms"]), int(x["exit_ms"]),
                             float(x["r"])))
    return sorted(out, key=lambda z: (z.t, z.bot != "breakout", z.coin))


def adx_lookup(c4h: pd.DataFrame) -> Callable[[int], float]:
    """ADX14 of the last 4h bar that had closed at time t."""
    from perpdesk.analysis.indicators import adx
    from perpdesk.timeutil import index_ms

    a = adx(c4h["high"], c4h["low"], c4h["close"], 14)["adx"].to_numpy()
    close_ms = index_ms(c4h) + H4

    def at(t: int) -> float:
        i = int(np.searchsorted(close_ms, t, side="right")) - 1
        return float(a[i]) if i >= 0 else float("nan")

    return at


def add_features(trades: list[T], frames_4h: dict[str, pd.DataFrame]) -> None:
    look = {c: adx_lookup(f) for c, f in frames_4h.items() if f is not None and not f.empty}
    for x in trades:
        if x.coin in look:
            x.adx = look[x.coin](x.t)
        if "BTC" in look:
            x.btc_adx = look["BTC"](x.t)


def replay(trades: list[T], rules: Rules) -> list[T]:
    taken: list[T] = []
    open_: list[T] = []
    for x in trades:
        open_ = [o for o in open_ if o.end > x.t]
        if any(o.coin == x.coin and o.bot == x.bot for o in open_):
            continue  # one position per coin per bot (as live)
        if sum(1 for o in open_ if o.bot == x.bot) >= rules.caps.get(x.bot, 3):
            continue
        if rules.no_coin_overlap and any(o.coin == x.coin for o in open_):
            continue
        if rules.max_same_dir is not None and sum(1 for o in open_ if o.direction == x.direction) >= rules.max_same_dir:
            continue
        if rules.max_open_r is not None and len(open_) + 1 > rules.max_open_r:
            continue
        if rules.min_adx is not None and not (x.adx >= rules.min_adx):
            continue
        if rules.min_btc_adx is not None and not (x.btc_adx >= rules.min_btc_adx):
            continue
        if rules.cooloff is not None:
            n, hours = rules.cooloff
            done = sorted((o for o in taken if o.direction == x.direction and o.end <= x.t), key=lambda o: o.end)[-n:]
            if len(done) == n and all(o.r < 0 for o in done) and x.t < done[-1].end + hours * 3_600_000:
                continue
        taken.append(x)
        open_.append(x)
    return taken


def _year(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, UTC).year


def _dd(rs: list[float]) -> float:
    eq = np.cumsum(rs) if rs else np.array([0.0])
    return float(np.max(np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq)) if rs else 0.0


def metrics(taken: list[T]) -> dict[str, Any]:
    by_exit = sorted(taken, key=lambda z: z.end)
    rs = [z.r for z in by_exit]
    total, dd = float(np.sum(rs)) if rs else 0.0, _dd(rs)
    streak = best = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        best = max(best, streak)
    worst30 = 0.0
    ends = [z.end for z in by_exit]
    for i in range(len(by_exit)):
        j = int(np.searchsorted(ends, ends[i] + 30 * DAY, side="left"))
        worst30 = min(worst30, float(np.sum(rs[i:j])))
    years: dict[str, Any] = {}
    for y in sorted({_year(z.end) for z in by_exit}):
        yr = [z.r for z in by_exit if _year(z.end) == y]
        years[str(y)] = {"n": len(yr), "sum_r": round(float(np.sum(yr)), 1), "dd": round(_dd(yr), 1)}
    halves = {"2021-23": [z.r for z in by_exit if _year(z.end) <= 2023],
              "2024-26": [z.r for z in by_exit if _year(z.end) >= 2024]}
    return {"n": len(rs), "win": round(float(np.mean([r > 0 for r in rs])), 3) if rs else 0.0,
            "sum_r": round(total, 1), "avg_r": round(total / len(rs), 3) if rs else 0.0, "max_dd": round(dd, 1),
            "ret_dd": round(total / dd, 2) if dd > 0 else None, "worst_30d": round(worst30, 1), "lose_streak": best,
            "years": years,
            "halves": {k: {"n": len(v), "sum_r": round(float(np.sum(v)), 1), "dd": round(_dd(v), 1)}
                       for k, v in halves.items()}}


def passes(base: dict[str, Any], v: dict[str, Any]) -> tuple[bool, list[str]]:
    """Pre-declared rule. A fix passes only if ALL hold versus BASE:
    1. return / max drawdown at least 10% better;   2. max drawdown not worse;
    3. better return/drawdown in at least 4 of the 6 calendar years (2021-2026);
    4. better return/drawdown in BOTH halves (2021-23 and 2024-26)."""
    why = []

    def rd(x: dict[str, Any]) -> float:
        return float(x["sum_r"]) / max(float(x["dd"]), 1.0)

    b_rd, v_rd = base["ret_dd"] or 0.0, v["ret_dd"] or 0.0
    ok1 = v_rd >= b_rd * 1.10 if b_rd > 0 else v_rd > b_rd
    why.append(f"return/DD {v['ret_dd']} vs {base['ret_dd']} ({'ok' if ok1 else 'not 10% better'})")
    ok2 = v["max_dd"] <= base["max_dd"]
    why.append(f"max DD {v['max_dd']}R vs {base['max_dd']}R ({'ok' if ok2 else 'worse'})")
    yrs = [y for y in base["years"] if y in v["years"]]
    better = sum(1 for y in yrs if rd(v["years"][y]) > rd(base["years"][y]))
    ok3 = better >= 4
    why.append(f"better in {better}/{len(yrs)} years ({'ok' if ok3 else 'needs 4'})")
    ok4 = all(rd(v["halves"][h]) > rd(base["halves"][h]) for h in base["halves"])
    why.append(f"both halves better: {'yes' if ok4 else 'no'}")
    return ok1 and ok2 and ok3 and ok4, why


def run_study(trades: list[T], wide: list[T] | None = None) -> dict[str, Any]:
    res: dict[str, Any] = {}
    base = metrics(replay(trades, VARIANTS["BASE (live rules today)"]))
    for name, rules in VARIANTS.items():
        m = base if name.startswith("BASE") else metrics(replay(trades, rules))
        res[name] = {"m": m, "pass": None if name.startswith("BASE") else passes(base, m)}
    if wide:
        m = metrics(replay(wide, Rules()))
        res["F2 breakout stop at least 1.5 ATR (was 0.8)"] = {"m": m, "pass": passes(base, m)}
    winners = [VARIANTS[n] for n, x in res.items() if x["pass"] and x["pass"][0] and n in VARIANTS]
    if len(winners) > 1:  # the passing fixes together (only fixes that passed alone)
        combo = Rules()
        for w in winners:
            for k, v in w.__dict__.items():
                if k != "caps" and v not in (None, False):
                    setattr(combo, k, v)
        m = metrics(replay(trades, combo))
        res["COMBO of the passing fixes"] = {"m": m, "pass": passes(base, m)}
    return res


def report_lines(res: dict[str, Any]) -> list[str]:
    out = ["PORTFOLIO STUDY: breakout + trend follower as one account, 1R per trade"]
    for name, x in res.items():
        m = x["m"]
        out.append(f"== {name}: n={m['n']} win={m['win']:.0%} sumR={m['sum_r']:+.1f} avgR={m['avg_r']:+.3f} "
                   f"maxDD={m['max_dd']}R return/DD={m['ret_dd']} worst30d={m['worst_30d']:+.1f}R "
                   f"longest losing run={m['lose_streak']}")
        out.append("   years: " + "  ".join(f"{y}: {v['sum_r']:+.1f} (DD {v['dd']}, n={v['n']})"
                                            for y, v in m["years"].items()))
        out.append("   halves: " + "  ".join(f"{h}: {v['sum_r']:+.1f} (DD {v['dd']})" for h, v in m["halves"].items()))
        if x["pass"] is not None:
            ok, why = x["pass"]
            out.append(f"   {'PASS' if ok else 'FAIL'}: " + "; ".join(why))
    return out


def main(breakout: str, lab: str, wide: str, cache: str, out: str) -> dict[str, Any]:
    from datetime import date

    from perpdesk.backtest.history import load_history

    trades = load_trades(Path(breakout), Path(lab))
    wide_tr = load_trades(Path(wide), Path(lab)) if wide and Path(wide).exists() else None
    coins = sorted({x.coin for x in trades} | {"BTC"})
    frames: dict[str, pd.DataFrame] = {}
    first = date(2020, 8, 1)
    for c in coins:
        try:
            frames[c] = load_history(c, first, datetime.now(UTC).date(), Path(cache)).frames()["4h"]
        except Exception as e:
            print(f"{c}: no 4h history for regime features ({e})")
    add_features(trades, frames)
    res = run_study(trades, wide_tr)
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "portfolio_study.json").write_text(json.dumps(res, indent=1, default=str))
    for line in report_lines(res):
        print(line, flush=True)
    return res
