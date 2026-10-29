"""Read-only 7-day audit of every paper-bot signal (TAKE and SKIP), what Telegram was told, what live/dry-run did,
and the user's own wallet trades. Each trade is re-checked against Hyperliquid 5m candles independently of the paper
simulator: which of stop / TP1 / TP2 price touched first, the best and worst move in R, and price 4h/24h later.

Run: python scripts/trades_report.py signals [days]   (prints lines starting with ROW/LIVE/WALLET/META + a JSON dump)
No addresses or keys are printed."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, "src")
from sqlalchemy import select

from perpdesk.db.models import AutoTrade, JobHeartbeat, WalletTrade
from perpdesk.db.session import session_scope
from perpdesk.sources.base import close_client
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import kv_get, settings_for

DAY = 86_400_000


def ts(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M") if ms else "-"


def path_check(t: AutoTrade, bars: Any, now_ms: int) -> dict[str, Any]:
    """Independent replay on 5m candles (no fees). R is measured from the fill (or planned entry) to the first stop."""
    out: dict[str, Any] = {}
    if bars is None or bars.empty:
        return {"err": "no candles"}
    d = 1 if t.direction == "LONG" else -1
    entry = t.fill_px or t.entry
    risk = abs(entry - t.sl)
    if risk <= 0:
        return {"err": "zero risk"}
    idx = [int(x.timestamp() * 1000) for x in bars.index]
    hi, lo, cl = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    start = t.fill_ms or t.created_ms
    # limit entry never filled: did price come back, and what would a market entry have done?
    end = t.exit_ms or now_ms
    sel = [i for i, m in enumerate(idx) if start <= m <= end]
    if sel:
        fav = max((hi[i] - entry) * d if d == 1 else (entry - lo[i]) for i in sel)
        adv = max((entry - lo[i]) if d == 1 else (hi[i] - entry) for i in sel)
        out["mfe_r"] = round(float(fav / risk), 2)
        out["mae_r"] = round(float(adv / risk), 2)
    # first touch among stop / tp1 / tp2 from the start, up to 7 days
    firsts: dict[str, int | None] = {"sl": None, "tp1": None, "tp2": None}
    for i, m in enumerate(idx):
        if m < start:
            continue
        for k, lvl in (("sl", t.sl), ("tp1", t.tp1), ("tp2", t.tp2)):
            if not lvl or firsts[k] is not None:
                continue
            hit = (lo[i] <= lvl) if (k == "sl") == (d == 1) else (hi[i] >= lvl)
            if hit:
                firsts[k] = m
    out["first_sl"], out["first_tp1"], out["first_tp2"] = ts(firsts["sl"]), ts(firsts["tp1"]), ts(firsts["tp2"])
    order = sorted((v, k) for k, v in firsts.items() if v is not None)
    out["touch_order"] = ">".join(k for _, k in order) or "none"
    if firsts["sl"] and firsts["tp1"] and firsts["sl"] == firsts["tp1"]:
        out["ambiguous"] = "stop and TP1 in the same 5m bar"
    for h in (4, 24):
        m = start + h * 3_600_000
        j = next((i for i, x in enumerate(idx) if x >= m), None)
        out[f"r_after_{h}h"] = round(float((cl[j] - entry) * d / risk), 2) if j is not None and m <= now_ms else None
    return out


async def main(days: int) -> None:
    hl = Hyperliquid()
    now = int(time.time() * 1000)
    since = now - days * DAY
    dump: dict[str, Any] = {"generated": ts(now), "days": days, "rows": [], "live": [], "wallet": []}
    with session_scope() as s:
        st = settings_for(s)
        meta = {
            "execution_mode": st.execution_mode, "live_mode": st.live_mode, "live_on": kv_get(s, "live:on"),
            "live_halt": kv_get(s, "live:halt"), "live_last": kv_get(s, "live:last"),
            "hl_key_set": bool(st.hl_agent_private_key), "hl_account_set": bool(st.hl_account_address),
            "auth": st.auth_enabled, "alert_account_usd": st.alert_account_usd, "alert_risk_pct": st.alert_risk_pct,
            "lab_bots": st.lab_bots, "breakout_short_take": st.breakout_short_take, "test_start": st.test_start,
            "quiet": st.alert_quiet_hours, "alerts_take": st.alerts_take, "watch_wallet_set": bool(st.watch_wallet),
        }
        for hb in s.execute(select(JobHeartbeat)).scalars():
            meta[f"hb_{hb.name}"] = f"{hb.last_run:%m-%d %H:%M} ok={hb.ok} {hb.detail[:100]}"
        dump["meta"] = meta
        print("META", json.dumps(meta, default=str))
        rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.created_ms >= since)
                         .order_by(AutoTrade.created_ms)).scalars().all()
        # also older trades still open or closed inside the window
        older = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.created_ms < since,
                                                  (AutoTrade.exit_ms >= since) | AutoTrade.status.in_(["pending", "open"])
                                                  )).scalars().all()
        cache: dict[str, Any] = {}
        for t in [*older, *rows]:
            pc: dict[str, Any] = {}
            if t.exit_model in ("fixed", "trail"):
                key = t.coin
                if key not in cache:
                    try:
                        cache[key] = await hl.candles(t.coin, "5m", max(since - 3 * DAY, now - 17 * DAY), now)
                    except Exception as e:
                        cache[key] = None
                        print("ERR candles", t.coin, e)
                pc = path_check(t, cache[key], now)
            r = {
                "id": t.id, "bot": t.setup, "coin": t.coin, "dir": t.direction, "created": ts(t.created_ms),
                "in_window": t.created_ms >= since, "status": t.status, "entry_type": t.entry_type,
                "entry": t.entry, "fill": t.fill_px, "fill_t": ts(t.fill_ms), "sl": t.sl, "tp1": t.tp1, "tp2": t.tp2,
                "stop_now": t.stop_now, "exit_model": t.exit_model, "exit_reason": t.exit_reason,
                "exit_t": ts(t.exit_ms), "exit_px": t.exit_px, "tp1_hit": t.tp1_hit, "r": t.r_multiple,
                "pnl_paper": t.pnl_usd, "risk_paper": t.risk_usd, "lev": t.leverage, "take": t.take,
                "take_reason": t.take_reason, "alert_open": ts(abs(t.alert_open_ms)) if t.alert_open_ms else None,
                "alert_suppressed": bool(t.alert_open_ms and t.alert_open_ms < 0),
                "alert_close": ts(t.alert_close_ms), "alert_fill": ts(t.alert_fill_ms), "alert_tp1": ts(t.alert_tp1_ms),
                "note": (t.note or "")[:80], **pc,
            }
            dump["rows"].append(r)
            print("ROW", json.dumps(r, default=str))
        for t in s.execute(select(AutoTrade).where(AutoTrade.mode.in_(["live", "dry"]))
                           .order_by(AutoTrade.created_ms)).scalars():
            r = {"mode": t.mode, "coin": t.coin, "dir": t.direction, "bot": t.setup, "created": ts(t.created_ms),
                 "status": t.status, "exit_reason": t.exit_reason, "pnl": t.pnl_usd, "note": (t.note or "")[:120]}
            dump["live"].append(r)
            print("LIVE", json.dumps(r, default=str))
        if st.watch_wallet:
            for w in s.execute(select(WalletTrade).where(WalletTrade.address == st.watch_wallet,
                                                         WalletTrade.opened_ms >= since)
                               .order_by(WalletTrade.opened_ms)).scalars():
                r = {"coin": w.coin, "dir": w.direction, "status": w.status, "opened": ts(w.opened_ms),
                     "closed": ts(w.closed_ms), "entry": w.entry_px, "exit": w.exit_px, "notional": round(w.notional, 2),
                     "lev": w.leverage, "net": round(w.net_pnl, 2), "fees": round(w.fees, 2), "sl_first": w.sl_first,
                     "exit_reason": w.exit_reason, "r": w.r_multiple, "plan_dir": w.plan_direction,
                     "plan_setup": w.plan_setup, "followed": w.followed}
                dump["wallet"].append(r)
                print("WALLET", json.dumps(r, default=str))
    await close_client()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
