"""Read-only diagnosis of the watched wallet's recent trades vs the app's signals (prints to stdout).

Syncs the wallet first, then for the most recent trades prints: the trade, the plan it was matched to, the
plan's own scored outcome, and what price did around the trade (1h candles + BTC). The address is never printed.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

sys.path.insert(0, "src")
from sqlalchemy import select

from perpdesk.db.models import JobHeartbeat, PlanOutcome, PlanRow, WalletTrade
from perpdesk.db.session import session_scope
from perpdesk.sources.base import close_client
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import kv_get, settings_for
from perpdesk.wallet.sync import sync_wallet

if len(sys.argv) > 1 and sys.argv[1].startswith("signals"):  # 7-day signal audit: "signals" or "signals:14"
    sys.path.insert(0, "scripts")
    from signals_report import main as signals_main

    asyncio.run(signals_main(int(sys.argv[1].split(":")[1]) if ":" in sys.argv[1] else 7))
    sys.exit(0)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 12


def ts(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M") if ms else "-"


def f(x: float | None, d: int = 6) -> str:
    return "-" if x is None else f"{x:.{d}g}"


async def main() -> None:
    hl = Hyperliquid()
    with session_scope() as s:
        st = settings_for(s)
        print("watched wallet set:", bool(st.watch_wallet), "| execution_mode:", st.execution_mode)
        for hb in s.execute(select(JobHeartbeat)).scalars():
            print(f"heartbeat {hb.name:6s} last_run={hb.last_run:%m-%d %H:%M} ok={hb.ok} {hb.detail[:120]}")
        lr = kv_get(s, "auto:last_run")
        print("auto last run:", (lr or {}).get("at"), "| opened:", (lr or {}).get("opened"))
        if not st.watch_wallet:
            return
        res = await sync_wallet(s, hl, st, force=True)
        s.commit()
        print(f"sync: new_fills={res.new_fills} opened={res.opened} closed={res.closed} error={res.error}\n")
        trades = s.execute(select(WalletTrade).where(WalletTrade.address == st.watch_wallet)
                           .order_by(WalletTrade.opened_ms.desc()).limit(N)).scalars().all()
        for t in trades:
            dur = ((t.closed_ms or 0) - t.opened_ms) / 3_600_000 if t.closed_ms else None
            sl_dist = abs((t.entry_px or 0) - (t.sl_first or 0)) / (t.entry_px or 1) * 100 if t.sl_first else None
            print(f"=== {t.coin} {t.direction} {t.status} opened {ts(t.opened_ms)} closed {ts(t.closed_ms)}"
                  f" ({f(dur, 3)}h)")
            print(f"  entry {f(t.entry_px)} exit {f(t.exit_px)} size {f(t.size)} notional ${t.notional:,.2f} "
                  f"lev {t.leverage}x liq {f(t.liq_px)}")
            print(f"  your SL first {f(t.sl_first)} ({f(sl_dist, 3)}% away) last {f(t.sl_px)} TP {f(t.tp_px)} "
                  f"| net ${t.net_pnl:+.2f} fees ${t.fees:.2f} (front-end ${t.builder_fees:.2f}) "
                  f"funding ${t.funding:+.2f} R {f(t.r_multiple, 3)} exit={t.exit_reason}")
            if t.plan_id:
                p = s.get(PlanRow, t.plan_id)
                if p is None:
                    continue
                o = s.get(PlanOutcome, p.id)
                print(f"  matched plan #{p.id} created {ts(p.created_ms)}: {p.direction} grade {p.grade} conf "
                      f"{p.confidence} setup {p.setup} tf {p.trading_tf} entry {p.entry_type} {f(p.entry)} "
                      f"zone {f(p.zone_lo)}-{f(p.zone_hi)} SL {f(p.sl)} TP1 {f(p.tp1)} TP2 {f(p.tp2)} "
                      f"rr {p.rr_tp1:.2f} lev {p.leverage}x win_prob {f(p.win_prob, 3)}")
                print(f"  plan flags: {[x.get('reason') for x in (p.flags or [])]}")
                print(f"  plan score {f((p.inputs or {}).get('score'), 3)} groups "
                      f"{ {k: round(v, 2) for k, v in ((p.inputs or {}).get('group_scores') or {}).items()} }")
                print(f"  plan's own outcome: {o.exit_reason if o else p.status} R "
                      f"{f(o.r_multiple if o else None, 3)} filled={o.filled if o else None} "
                      f"fill {ts(o.fill_ms) if o else '-'} exit {ts(o.exit_ms) if o else '-'}")
                print(f"  followed: {t.followed} | your entry vs plan entry "
                      f"{((t.entry_px or 0) / p.entry - 1) * 100:+.2f}% | your SL vs plan SL "
                      f"{(((t.sl_first or p.sl) / p.sl) - 1) * 100:+.2f}%")
            else:
                print("  no matching plan (no signal for this coin in the 12h before the trade)")
            end = (t.closed_ms or t.opened_ms + 12 * 3_600_000) + 6 * 3_600_000
            for coin in (t.coin, "BTC"):
                try:
                    c = await hl.candles(coin, "1h", t.opened_ms - 6 * 3_600_000, end)
                except Exception as e:
                    print(f"  {coin} candles unavailable: {e}")
                    continue
                if c.empty:
                    continue
                first = float(c["open"].iloc[0])
                path = " ".join(f"{float(x):.5g}" for x in c["close"].iloc[::2])
                print(f"  {coin} 1h closes (every 2h, from 6h before open): {path}")
                print(f"  {coin} range in window: low {float(c['low'].min()):.5g} high {float(c['high'].max()):.5g} "
                      f"move {(float(c['close'].iloc[-1]) / first - 1) * 100:+.2f}%")
            print()
        recent = s.execute(select(PlanRow).order_by(PlanRow.created_ms.desc()).limit(30)).scalars().all()
        print("--- last 30 logged plans (all coins)")
        for p in recent:
            o = s.get(PlanOutcome, p.id)
            print(f"  #{p.id} {ts(p.created_ms)} {p.coin:5s} {p.direction:5s} {p.grade} conf {p.confidence:3d} "
                  f"{p.setup:15s} {p.trading_tf} rr {p.rr_tp1:.2f} -> {o.exit_reason if o else p.status} "
                  f"R {f(o.r_multiple if o else None, 3)}")
    await close_client()


asyncio.run(main())
