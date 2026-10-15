"""Print the latest backtest's per-setup statistics and confidence/grade breakdown from DATABASE_URL."""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, "src")
from sqlalchemy import func, select

from perpdesk.db.models import BacktestRun, BacktestTrade, SetupStat
from perpdesk.db.session import session_scope

with session_scope() as s:
    run = s.execute(select(func.max(BacktestRun.id))).scalar()
    print("run", run)
    rows = s.execute(select(SetupStat).where(SetupStat.run_id == run)).scalars().all()
    print(f"{'setup':16s} {'scope':10s} {'dir':5s} {'n':>5s} {'win':>5s} {'avgR':>7s} {'PF':>6s} {'DD':>6s} {'+q':>4s} gate")
    for r in sorted(rows, key=lambda x: (x.scope_kind != "all", x.setup, x.scope, x.direction)):
        if r.scope_kind == "class":
            continue
        st = r.stats
        pf = st.get("profit_factor") or 0
        print(f"{r.setup:16s} {r.scope:10s} {r.direction:5s} {st.get('n', 0):>5d} {st.get('win_rate', 0):>5.0%} "
              f"{st.get('avg_r', 0):>+7.3f} {min(pf, 99):>6.2f} {st.get('max_dd_r', 0):>6.1f} "
              f"{st.get('positive_quarters', 0):>2d}/{len(st.get('quarters', []))} "
              f"{'PASS' if r.gate_passed else (r.gate_reasons[0] if r.gate_reasons else '')[:60]}")
    trades = s.execute(select(BacktestTrade).where(BacktestTrade.run_id == run)).scalars().all()
    for label, key in (("confidence bucket", lambda t: (t.confidence // 10) * 10), ("grade", lambda t: t.grade_ex_gate),
                       ("period", lambda t: t.period)):
        g: dict = defaultdict(list)
        for t in trades:
            if t.setup == "composite" or label == "period":
                g[key(t)].append(t)
        print(f"\n-- {'composite' if label != 'period' else 'all setups'} by {label}")
        for k in sorted(g):
            ts = g[k]
            rs = [t.r_multiple for t in ts]
            print(f"{k!s:>8} n={len(ts):5d} win={sum(r > 0 for r in rs) / len(rs):5.0%} "
                  f"tp1={sum(t.tp1_before_sl for t in ts) / len(ts):5.0%} avgR={sum(rs) / len(rs):+.3f}")
