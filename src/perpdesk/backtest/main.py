"""Backtest orchestration: history -> pass 1 per coin -> walk-forward -> gates -> report (+ DB)."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from jinja2 import Environment

from perpdesk.analysis.engine import EngineConfig
from perpdesk.analysis.setups import PARAM_GRIDS, SETUP_LABELS
from perpdesk.analysis.types import AssetMeta
from perpdesk.assets.registry import class_for
from perpdesk.backtest.history import History, load_context_history, load_history
from perpdesk.backtest.runner import CoinRun, Trade, ablation, apply_confidence, pass1, score_quantiles, walk_forward
from perpdesk.config import Settings
from perpdesk.plan import PlanConfig
from perpdesk.timeutil import index_ms
from perpdesk.tracking.calibration import fit
from perpdesk.tracking.gates import GateConfig, evaluate
from perpdesk.tracking.stats import r_stats


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def _meta_for(coin: str, hist: History) -> AssetMeta:
    # size/leverage metadata does not affect R outcomes; use permissive values
    return AssetMeta(coin=coin, is_perp=True, sz_decimals=4, max_leverage=20, margin_tiers=[], asset_class="")


def minute_loader(coin: str, cache_dir: Path) -> Callable[[int, int], pd.DataFrame | None]:
    """1-minute Binance candles for a time range (daily archive files, cached on disk)."""
    from perpdesk.sources.binance_hist import load_days

    def load(start_ms: int, end_ms: int) -> pd.DataFrame | None:
        days = [d.date() for d in pd.date_range(pd.Timestamp(start_ms, unit="ms", tz="UTC").normalize(),
                                                pd.Timestamp(end_ms, unit="ms", tz="UTC").normalize(), freq="D")]
        try:
            df = load_days(coin, "1m", days, cache_dir / "1m")
        except Exception as e:
            _log(f"{coin}: 1m candles unavailable ({type(e).__name__}); keeping the 5m result")
            return None
        ms = index_ms(df)
        return df[(ms >= start_ms) & (ms < end_ms)]

    return load


def yearly_lines(trades: list[Trade]) -> list[str]:
    """Out-of-sample avg R per setup × direction × calendar year: shows bull vs bear behaviour."""
    by: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for t in trades:
        if t.period != "train" and t.filled and t.r is not None:
            by[(t.setup, t.direction, datetime.fromtimestamp(t.t_ms / 1000, UTC).year)].append(float(t.r))
    out = ["OOS + hold-out by year:"]
    for (setup, d, y), v in sorted(by.items()):
        out.append(f"  {setup:15s} {d:5s} {y}  n={len(v):4d} avgR={float(np.mean(v)):+.3f} "
                   f"sumR={float(np.sum(v)):+.1f}")
    return out


def run_backtest(coins: list[str], start: date, end: date, settings: Settings, cache_dir: Path, out_dir: Path,
                 classes: dict[str, str] | None = None, do_ablation: bool = False, write_db: bool = False
                 ) -> dict[str, Any]:
    t0 = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    from perpdesk.pipeline import engine_config, plan_config

    ecfg: EngineConfig = engine_config(settings)
    fg, cme = load_context_history()
    _log(f"context: fear&greed {'ok' if fg is not None else 'unavailable'}, CME {'ok' if cme is not None else 'unavailable'}")
    # indicators need ~220 daily bars of warm-up: load it before `start` so the full requested
    # window is analysed (and enough out-of-sample quarters exist for the stability gate)
    warm = start - timedelta(days=300)
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
    btc_hist = load_history("BTC", warm, end, cache_dir)
    btc_frames = btc_hist.frames()
    runs: list[CoinRun] = []
    pcfgs: dict[str, PlanConfig] = {}
    for coin in coins:
        cls = (classes or {}).get(coin) or class_for(coin, 20 if coin in ("BTC", "ETH") else 10)
        hist = btc_hist if coin == "BTC" else load_history(coin, warm, end, cache_dir)
        if hist.c1h.empty or hist.c5m.empty:
            _log(f"{coin}: no history available, skipped ({hist.sources})")
            continue
        _log(f"{coin}: 1h bars {len(hist.c1h)}, 5m bars {len(hist.c5m)}, sources {hist.sources}")
        pcfg = plan_config(settings, cls)
        pcfgs[coin] = pcfg
        meta = _meta_for(coin, hist)
        ts = time.monotonic()
        recs = pass1(coin, cls, meta, hist.frames(), None if coin == "BTC" else btc_frames, hist.funding, fg, cme,
                     ecfg, pcfg, start_ms=start_ms, progress=_log)
        if not recs:
            _log(f"{coin}: not enough history after warm-up, skipped")
            continue
        runs.append(CoinRun(coin, cls, recs, hist.c5m, hist.sources, time.monotonic() - ts, recs[0].t_ms,
                            recs[-1].t_ms, fine=minute_loader(coin, cache_dir)))
        _log(f"{coin}: pass 1 done, {len(recs)} bars in {time.monotonic() - ts:.0f}s")
    if not runs:
        raise RuntimeError("no coin had usable history")
    pcfg_any = next(iter(pcfgs.values()))
    quants = score_quantiles(runs)
    apply_confidence(runs, quants, settings.min_confidence)
    _log(f"confidence: percentile of |score| over {sum(len(r.records) for r in runs)} bars; "
         f"median |score| {quants[50] if quants else 'n/a'}, 60th pct {quants[60] if quants else 'n/a'}")
    wf, trades = walk_forward(runs, pcfg_any)
    _log(f"walk-forward: {len(wf.windows)} windows, {len(trades)} labelled trades, {wf.combos_tried} combinations tried")
    gcfg = GateConfig(settings.gate_min_trades, settings.gate_min_expectancy_r, settings.gate_min_pf,
                      settings.gate_max_dd_r)
    stats_rows = compute_stats(trades, gcfg, wf, settings)
    _log("OOS results (all coins pooled and per coin):")
    for r in sorted(stats_rows, key=lambda x: (x["scope_kind"] != "all", x["setup"], x["scope"], x["direction"])):
        if r["scope_kind"] == "class":
            continue
        st = r["stats"]
        pf = st.get("profit_factor") or 0
        _log(f"  {r['setup']:15s} {r['scope']:5s} {r['direction']:5s} n={st.get('n', 0):5d} win={st.get('win_rate', 0):4.0%} "
             f"avgR={st.get('avg_r', 0):+.3f} PF={min(pf, 99):.2f} DD={st.get('max_dd_r', 0):.1f} "
             f"{'PASS' if r['gate_passed'] else 'fail: ' + (r['gate_reasons'][0] if r['gate_reasons'] else '')}")
    by_period: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.filled and t.r is not None:
            by_period[t.period].append(float(t.r))
    for k, v in sorted(by_period.items()):
        _log(f"  period {k:8s} n={len(v):5d} avgR={float(np.mean(v)):+.3f}")
    _log(f"ambiguous exits re-checked on 1m candles: {sum(r.refined for r in runs)} "
         f"(result changed for {sum(r.flipped for r in runs)})")
    for line in yearly_lines(trades):
        _log(line)
    abl: dict[str, list[dict[str, Any]]] = {}
    if do_ablation:
        for run in runs:
            hist = btc_hist if run.coin == "BTC" else load_history(run.coin, warm, end, cache_dir)
            abl[run.coin] = ablation(run.coin, run.asset_class, _meta_for(run.coin, hist), hist.frames(),
                                     None if run.coin == "BTC" else btc_frames, hist.funding, fg, cme, ecfg,
                                     pcfgs[run.coin], hist.c5m, [g for g, w in ecfg.group_weights.items() if w > 0])
            _log(f"{run.coin}: ablation done")
    summary = {
        "start": min(datetime.fromtimestamp(r.first_ms / 1000, UTC) for r in runs).strftime("%Y-%m-%d"),
        "end": max(datetime.fromtimestamp(r.last_ms / 1000, UTC) for r in runs).strftime("%Y-%m-%d"),
        "coins": [r.coin for r in runs], "trades": sum(1 for t in trades if t.period == "oos" and t.filled),
        "gates_passed": sum(1 for s in stats_rows if s["gate_passed"]), "windows": len(wf.windows),
        "combos_tried": wf.combos_tried, "seconds": round(time.monotonic() - t0),
        "final_params": {k: PARAM_GRIDS[k][v] for k, v in wf.final_params.items() if k in PARAM_GRIDS},
        "holdout_start": datetime.fromtimestamp(wf.holdout_start / 1000, UTC).strftime("%Y-%m-%d"),
        "score_quantiles": list(quants),
    }
    html = render_report(runs, trades, stats_rows, wf, summary, abl, settings)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    path = out_dir / f"backtest-{'-'.join(r.coin for r in runs)}-{stamp}.html"
    path.write_text(html)
    (out_dir / f"backtest-{stamp}.json").write_text(json.dumps({"summary": summary, "stats": stats_rows}, default=str,
                                                               indent=1))
    _log(f"report written: {path}")
    run_id = None
    if write_db:
        run_id = save_to_db(runs, trades, stats_rows, summary, html, settings)
        _log(f"saved to database as run #{run_id}")
    return {"summary": summary, "report": str(path), "run_id": run_id, "stats": stats_rows}


def compute_stats(trades: list[Trade], gcfg: GateConfig, wf: Any, settings: Settings) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[Trade]] = defaultdict(list)
    for t in trades:
        if t.period != "oos" or not t.filled or t.r is None:
            continue
        for kind, scope in (("asset", t.coin), ("class", t.asset_class), ("all", "*")):
            groups[(t.setup, kind, scope, t.direction)].append(t)
    rows = []
    for (setup, kind, scope, direction), trs in sorted(groups.items()):
        trs.sort(key=lambda x: x.t_ms)
        res = evaluate([t.t_ms for t in trs], [float(t.r or 0) for t in trs], [t.rr_tp1 for t in trs],
                       [t.tp1_before_sl for t in trs], gcfg)
        if setup == "composite":
            res.passed = False
            res.reasons = ["composite bias is never eligible for grade A"] + res.reasons
        cal = fit([t.confidence for t in trs], [t.tp1_before_sl for t in trs])
        idx = wf.final_params.get(setup, 0)
        params = PARAM_GRIDS[setup][idx] if setup in PARAM_GRIDS else {}
        rows.append({"setup": setup, "scope_kind": kind, "scope": scope, "direction": direction, "params": params,
                     "stats": _clean(res.stats), "gate_passed": res.passed, "gate_reasons": res.reasons,
                     "calibration": cal.to_json()})
    return rows


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and not math.isfinite(v):
            v = 1e9 if v > 0 else None
        out[k] = v
    return out


def save_to_db(runs: list[CoinRun], trades: list[Trade], stats_rows: list[dict[str, Any]], summary: dict[str, Any],
               html: str, settings: Settings) -> int:
    from perpdesk.db.models import BacktestRun, BacktestTrade, SetupStat
    from perpdesk.db.session import migrate, session_scope

    migrate()
    with session_scope() as s:
        run = BacktestRun(coins=[r.coin for r in runs], params={"settings": {
            k: getattr(settings, k) for k in ("trading_timeframe", "min_rr", "sl_atr_buffer", "min_sl_atr",
                                              "plan_ttl_min", "max_hold_hours", "taker_fee_bps", "maker_fee_bps")}},
                          summary=summary, report_html=html)
        s.add(run)
        s.flush()
        s.add_all([BacktestTrade(run_id=run.id, setup=t.setup, coin=t.coin, asset_class=t.asset_class,
                                 direction=t.direction, ts=t.t_ms, confidence=t.confidence,
                                 grade_ex_gate=t.grade_ex_gate, rr_tp1=t.rr_tp1, r_multiple=float(t.r),
                                 tp1_before_sl=t.tp1_before_sl, period=t.period)
                   for t in trades if t.filled and t.r is not None])
        s.add_all([SetupStat(run_id=run.id, setup=r["setup"], scope_kind=r["scope_kind"], scope=r["scope"],
                             direction=r["direction"], params=r["params"], stats=r["stats"],
                             gate_passed=r["gate_passed"], gate_reasons=r["gate_reasons"], calibration=r["calibration"])
                   for r in stats_rows])
        return int(run.id)


# ---------------------------------------------------------------- HTML report

REPORT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PerpDesk backtest {{ s.start }} → {{ s.end }}</title>
<style>
:root{--bg:#09090b;--card:#18181b;--line:#27272a;--text:#e4e4e7;--muted:#a1a1aa;--accent:#818cf8;--up:#34d399;--down:#fb7185;--warn:#fbbf24}
body{background:var(--bg);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif;margin:0;padding:16px}
main{max-width:1100px;margin:0 auto} h1{font-size:22px} h2{font-size:16px;margin-top:28px} h3{font-size:14px;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:12px} th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left} th{color:var(--muted);font-weight:500}
.up{color:var(--up)} .down{color:var(--down)} .muted{color:var(--muted)} .warn{background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.3);color:#fde68a;border-radius:8px;padding:8px 12px;margin:8px 0}
.pass{color:var(--accent);font-weight:600} li{margin:4px 0}
</style></head><body><main>
<h1>PerpDesk backtest</h1>
<p class="muted">{{ s.coins|join(', ') }} · {{ s.start }} → {{ s.end }} · trading timeframe {{ settings.trading_timeframe }} · generated {{ now }} UTC in {{ s.seconds }}s</p>
<div class="card">
<b>{{ s.trades }}</b> out-of-sample filled trades · <b>{{ s.gates_passed }}</b> setup/scope/direction combinations passed every gate ·
{{ s.windows }} walk-forward windows (6-month train / 3-month test) · <b>{{ s.combos_tried }}</b> parameter combinations tried in total ·
hold-out from {{ s.holdout_start }} (evaluated once).<br>
Parameters used live (fit on all pre-hold-out data): {% for k, v in s.final_params.items() %}{{ labels.get(k, k) }} {{ v }}{% if not loop.last %}; {% endif %}{% endfor %}
</div>
{% if s.trades < settings.gate_min_trades %}<div class="warn">The out-of-sample sample is small ({{ s.trades }} trades). Do not draw conclusions from these numbers.</div>{% endif %}

<h2>Setup gates (out-of-sample)</h2>
<div class="card"><table><tr><th>Setup</th><th>Scope</th><th>Dir</th><th>n</th><th>Win</th><th>Wilson low</th><th>Break-even</th><th>Avg R</th><th>PF</th><th>Max DD</th><th>+Quarters</th><th>Gate</th></tr>
{% for r in stats %}{% set x = r.stats %}<tr>
<td>{{ labels.get(r.setup, r.setup) }}</td><td>{{ r.scope }}</td><td>{{ r.direction }}</td><td>{{ x.n }}</td><td>{{ pct(x.win_rate) }}</td><td>{{ pct(x.wilson_lo) }}</td><td>{{ pct(x.be_wr) }}</td>
<td class="{{ 'up' if x.avg_r > 0 else 'down' }}">{{ '%+.2f'|format(x.avg_r) }}</td><td>{{ pf(x.profit_factor) }}</td><td>{{ '%.1f'|format(x.max_dd_r) }}R</td><td>{{ x.positive_quarters }}/{{ x.quarters|length }}</td>
<td>{% if r.gate_passed %}<span class="pass">PASS → A</span>{% else %}<span class="muted" title="{{ r.gate_reasons|join('; ') }}">{{ r.gate_reasons[0] if r.gate_reasons else 'fail' }}</span>{% endif %}</td></tr>{% endfor %}
</table></div>

<h2>Per asset</h2>
{% for c in per_coin %}<div class="card"><h3>{{ c.coin }} ({{ c.cls }})</h3>
<p class="muted">Data: {% for k, v in c.sources.items() %}{{ k }}: {{ v }}{% if not loop.last %} · {% endif %}{% endfor %}<br>{{ c.bars }} bars analysed in {{ c.secs }}s.</p>
<table><tr><th>Strategy</th><th>OOS trades</th><th>Win</th><th>Avg R</th><th>PF</th><th>Max DD</th><th>Not triggered</th><th>Hold-out n</th><th>Hold-out avg R</th></tr>
{% for st in c.strategies %}<tr><td>{{ labels.get(st.setup, st.setup) }}</td><td>{{ st.n }}</td><td>{{ pct(st.win) }}</td><td class="{{ 'up' if st.avg > 0 else 'down' }}">{{ '%+.2f'|format(st.avg) }}</td><td>{{ pf(st.pf) }}</td><td>{{ '%.1f'|format(st.dd) }}R</td><td>{{ st.nt }}</td><td>{{ st.hn }}</td><td>{{ '%+.2f'|format(st.havg) if st.hn else '–' }}</td></tr>{% endfor %}
</table>
{% if c.small %}<div class="warn">Sample too small to conclude anything for {{ c.coin }}.</div>{% endif %}
</div>{% endfor %}

<h2>Composite bias by confidence bucket (out-of-sample)</h2>
<div class="card"><table><tr><th>Confidence</th><th>n</th><th>TP1 before SL</th><th>Win</th><th>Avg R</th></tr>
{% for b in conf %}<tr><td>{{ b.k }}–{{ b.k + 9 }}</td><td>{{ b.n }}</td><td>{{ pct(b.tp1) }}</td><td>{{ pct(b.win) }}</td><td class="{{ 'up' if b.avg > 0 else 'down' }}">{{ '%+.2f'|format(b.avg) }}</td></tr>{% endfor %}</table></div>

<h2>By grade (composite bias, out-of-sample)</h2>
<div class="card"><table><tr><th>Grade</th><th>n</th><th>Win</th><th>Avg R</th></tr>
{% for b in grades %}<tr><td>{{ b.k }}</td><td>{{ b.n }}</td><td>{{ pct(b.win) }}</td><td class="{{ 'up' if b.avg > 0 else 'down' }}">{{ '%+.2f'|format(b.avg) }}</td></tr>{% endfor %}</table>
<p class="muted">Backtest grades exclude A by construction (A requires these gates); B/C follow the confidence threshold.</p></div>

{% if abl %}<h2>Factor-group ablation (composite, every 4th bar)</h2>
{% for coin, rows in abl.items() %}<div class="card"><h3>{{ coin }}</h3><table><tr><th>Variant</th><th>n</th><th>Win</th><th>Avg R</th></tr>
{% for r in rows %}<tr><td>{{ r.variant }}</td><td>{{ r.n }}</td><td>{{ pct(r.win) }}</td><td>{{ '%+.3f'|format(r.avg_r) if r.avg_r == r.avg_r else '–' }}</td></tr>{% endfor %}</table>
<p class="muted">If removing a group improves expectancy, that group is not helping on this asset.</p></div>{% endfor %}{% endif %}

<h2>Method and limitations</h2>
<div class="card"><ul>
<li>Same engine and scorer as live. At each {{ settings.trading_timeframe }} close, only data available at that moment is used (indicators are causal, higher-timeframe bars included only once closed; verified by an automated no-lookahead test).</li>
<li>Fills and exits are simulated on 5-minute candles: limit entries fill only if price trades through the entry; if a stop and a target fall inside the same candle the stop is assumed first; exits pay taker fees ({{ settings.taker_fee_bps }} bps) plus slippage ({{ settings.slippage_bps_major }} bps majors / {{ settings.slippage_bps_alt }} bps alts); hourly Hyperliquid funding is charged over the holding period.</li>
<li>Price history before Hyperliquid's ~5,000-bar window comes from Binance USDT-M futures (data.binance.vision). Binance and Hyperliquid prices for these coins differ by a few basis points; funding always comes from Hyperliquid.</li>
<li>Open-interest factors and Coinglass liquidation levels are information-only (not scored) because no free historical source exists; the event-calendar blackout cannot be backtested (no free history) and only affects position size, not R outcomes.</li>
<li>Each setup is tested in isolation with one position at a time per asset; live trading logs one active plan per asset across all setups, so live trade counts will be lower.</li>
<li>Parameter grids are tiny ({{ grid_sizes }}) and chosen on pooled train data across all assets; no per-asset tuning. The total number of combinations tried is reported above. The hold-out period was not used for any selection.</li>
<li>Past performance does not guarantee future results; regime changes can invalidate any statistical edge. Live performance is tracked separately and can demote a setup.</li>
</ul></div>
</main></body></html>"""


def render_report(runs: list[CoinRun], trades: list[Trade], stats_rows: list[dict[str, Any]], wf: Any,
                  summary: dict[str, Any], abl: dict[str, list[dict[str, Any]]], settings: Settings) -> str:
    def pct(x: Any) -> str:
        return "–" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{float(x) * 100:.0f}%"

    def pf(x: Any) -> str:
        if x is None:
            return "–"
        return "∞" if x >= 1e9 else f"{x:.2f}"

    per_coin = []
    for run in runs:
        strategies: list[dict[str, Any]] = []
        for setup in list(PARAM_GRIDS) + ["composite"]:
            oos = [t for t in trades if t.coin == run.coin and t.setup == setup and t.period == "oos"]
            filled = [t.r for t in oos if t.filled and t.r is not None]
            hold = [t.r for t in trades if t.coin == run.coin and t.setup == setup and t.period == "holdout"
                    and t.filled and t.r is not None]
            st = r_stats([float(x) for x in filled if x is not None])
            strategies.append({"setup": setup, "n": st.n, "win": st.win_rate, "avg": st.avg_r, "pf": st.profit_factor
                               if st.profit_factor is None or math.isfinite(st.profit_factor) else 1e9,
                               "dd": st.max_dd_r, "nt": sum(1 for t in oos if not t.filled), "hn": len(hold),
                               "havg": float(np.mean([h for h in hold if h is not None])) if hold else 0.0})
        n_comp = int(next(x["n"] for x in strategies if x["setup"] == "composite"))
        per_coin.append({"coin": run.coin, "cls": run.asset_class, "sources": run.sources, "bars": len(run.records),
                         "secs": round(run.seconds), "strategies": strategies, "small": n_comp < 30})
    comp = [t for t in trades if t.setup == "composite" and t.period == "oos" and t.filled and t.r is not None]
    conf, grades = [], []
    by_conf: dict[int, list[Trade]] = defaultdict(list)
    by_grade: dict[str, list[Trade]] = defaultdict(list)
    for t in comp:
        by_conf[(t.confidence // 10) * 10].append(t)
        by_grade[t.grade_ex_gate].append(t)
    for kc in sorted(by_conf):
        ts = by_conf[kc]
        conf.append({"k": kc, "n": len(ts), "tp1": np.mean([t.tp1_before_sl for t in ts]),
                     "win": np.mean([(t.r or 0) > 0 for t in ts]), "avg": np.mean([t.r or 0 for t in ts])})
    for kg in sorted(by_grade):
        ts = by_grade[kg]
        grades.append({"k": kg, "n": len(ts), "win": np.mean([(t.r or 0) > 0 for t in ts]),
                       "avg": np.mean([t.r or 0 for t in ts])})
    env = Environment(autoescape=True)
    tpl = env.from_string(REPORT)
    grid_sizes = ", ".join(f"{SETUP_LABELS[k]}: {len(v)}" for k, v in PARAM_GRIDS.items())
    stats_sorted = sorted(stats_rows, key=lambda r: (not r["gate_passed"], r["setup"], r["scope_kind"], r["scope"]))
    return tpl.render(s=summary, stats=stats_sorted, per_coin=per_coin, conf=conf, grades=grades, abl=abl,
                      settings=settings, labels=SETUP_LABELS, pct=pct, pf=pf, grid_sizes=grid_sizes,
                      now=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"))


def asdict_safe(x: Any) -> dict[str, Any]:
    return asdict(x)
