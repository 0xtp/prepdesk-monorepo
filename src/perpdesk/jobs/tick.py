"""Scheduled jobs. `tick` runs every few minutes (cron endpoint / GitHub Actions / CLI); `daily` once a day.

tick:  score open plans -> refresh market context -> analyse favourites in batches within a time
       budget (resuming from a cursor on the next tick) -> log plans -> alerts -> health + heartbeat.
daily: refresh perp metadata, live-gate check, prune old data, sync Anthropic org cost (if admin key).
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from perpdesk.alerts import telegram
from perpdesk.assets.favorites import list_favorites, seed_defaults
from perpdesk.assets.registry import refresh_perp_meta
from perpdesk.db.models import BacktestTrade, Candle, CtxSnapshot, JobHeartbeat, SetupStat
from perpdesk.pipeline import analyse_asset, load_context, now_ms
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import heartbeat, persist_health, settings_for
from perpdesk.tracking import service
from perpdesk.tracking.gates import live_check
from perpdesk.tracking.live import score_open_plans


async def tick(s: Session, budget_s: float = 50.0) -> dict[str, Any]:
    t0 = time.monotonic()
    settings = settings_for(s)
    hl = Hyperliquid(settings.hyperliquid_api_url, settings.hl_rate_budget_per_min)
    out: dict[str, Any] = {}
    try:
        out["seed_missing"] = await seed_defaults(s, settings, hl)
        out["scoring"] = await score_open_plans(s, hl, now_ms())
        s.commit()
        ctx = await load_context(s, hl, settings)
        s.commit()
        favs = list_favorites(s)
        hb = s.get(JobHeartbeat, "tick")
        cursor = hb.cursor if hb else 0
        n = len(favs)
        done, errors = [], []
        order = favs[cursor % n:] + favs[: cursor % n] if n else []
        for fav in order:
            if time.monotonic() - t0 > budget_s:
                break
            res = await analyse_asset(s, hl, fav.asset, ctx, settings, log=True)
            s.commit()
            done.append(fav.asset.symbol)
            if res.error:
                errors.append(f"{fav.asset.symbol}: {res.error}")
            elif res.plan is not None:
                await telegram.maybe_alert(s, settings, res.plan, res.view)
        new_cursor = (cursor + len(done)) % n if n else 0
        out.update({"analysed": done, "errors": errors, "remaining": n - len(done)})
        persist_health(s)
        heartbeat(s, "tick", not errors, int((time.monotonic() - t0) * 1000),
                  f"{len(done)}/{n} assets; {len(errors)} errors", new_cursor)
        s.commit()
    except Exception as e:
        s.rollback()
        heartbeat(s, "tick", False, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {e}")
        s.commit()
        raise
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


async def daily(s: Session) -> dict[str, Any]:
    t0 = time.monotonic()
    settings = settings_for(s)
    hl = Hyperliquid(settings.hyperliquid_api_url, settings.hl_rate_budget_per_min)
    out: dict[str, Any] = {}
    await refresh_perp_meta(s, hl)
    # live gate: demote A setups whose live performance diverges from the backtest
    run = service.latest_run_id(s)
    demoted = []
    if run is not None:
        for row in s.execute(select(SetupStat).where(SetupStat.run_id == run, SetupStat.gate_passed.is_(True))).scalars():
            filt: dict[str, object] = {"setup": row.setup, "direction": row.direction}
            if row.scope_kind == "asset":
                filt["coin"] = row.scope
            elif row.scope_kind == "class":
                filt["asset_class"] = row.scope
            lrs = service.live_rs(s, **filt)
            q = select(BacktestTrade.r_multiple).where(BacktestTrade.run_id == run, BacktestTrade.setup == row.setup,
                                                       BacktestTrade.direction == row.direction,
                                                       BacktestTrade.period == "oos")
            if row.scope_kind == "asset":
                q = q.where(BacktestTrade.coin == row.scope)
            elif row.scope_kind == "class":
                q = q.where(BacktestTrade.asset_class == row.scope)
            oos = [float(x) for x in s.execute(q).scalars()]
            chk = live_check(lrs, oos, settings.min_sample_for_stats)
            if chk.demote and not row.live_demoted:
                demoted.append(f"{row.setup}/{row.scope}/{row.direction}")
            # promote back only by re-passing the gates in a new backtest run
            if chk.demote:
                row.live_demoted, row.live_reason = True, chk.reason
    out["demoted"] = demoted
    # prune: keep candles bounded, snapshots 30 days
    cutoff = now_ms() - 30 * 86_400_000
    s.execute(delete(CtxSnapshot).where(CtxSnapshot.ts < cutoff))
    s.execute(delete(Candle).where(Candle.tf.in_(["1m", "5m"]), Candle.ts < now_ms() - 7 * 86_400_000))
    if settings.anthropic_admin_key:
        from perpdesk.llm.usage import sync_org_cost

        out["org_cost"] = await sync_org_cost(s, settings)
    persist_health(s)
    heartbeat(s, "daily", True, int((time.monotonic() - t0) * 1000), str(out)[:500])
    s.commit()
    return out
