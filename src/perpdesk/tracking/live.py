"""Score live plans against real candles (Hyperliquid 1m; 5m/15m fallback for older plans)."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.db.models import PlanOutcome, PlanRow
from perpdesk.sources import coingecko
from perpdesk.sources.base import guarded
from perpdesk.sources.hyperliquid import Hyperliquid, closed_only
from perpdesk.tracking.scorer import Outcome, PlanSpec, score


def spec_of(p: PlanRow) -> PlanSpec:
    c = (p.inputs or {}).get("costs", {})
    taker, maker, slip = float(c.get("taker_bps", 4.5)), float(c.get("maker_bps", 1.5)), float(c.get("slip_bps", 3))
    entry_fee = (maker if p.entry_type == "limit" else taker + slip) / 1e4
    hold = int((p.inputs or {}).get("max_hold_hours", 48))
    return PlanSpec(direction=p.direction, entry_type=p.entry_type, entry=p.entry, sl=p.sl, tp1=p.tp1, tp2=p.tp2,
                    runaway=p.runaway, created_ms=p.created_ms, expires_ms=p.expires_ms, max_hold_ms=hold * 3_600_000,
                    entry_fee=entry_fee, exit_fee=taker / 1e4, slippage=slip / 1e4)


def resolution_for(age_ms: int) -> str:
    if age_ms <= 3 * 86_400_000:
        return "1m"
    if age_ms <= 16 * 86_400_000:
        return "5m"
    return "15m"


async def _bars(hl: Hyperliquid, p: PlanRow, start: int, end: int, res: str) -> pd.DataFrame | None:
    spot_series = (p.inputs or {}).get("spot_series")
    if spot_series and str(spot_series).startswith("gt:"):
        _, chain, pool = str(spot_series).split(":", 2)
        r = await guarded("geckoterminal", lambda: coingecko.pool_ohlcv(chain, pool, "minute", 15, 1000))
        return closed_only(r.data, "15m", end) if r.ok and r.data is not None else None
    coin = spot_series if spot_series else p.coin
    r = await guarded("hyperliquid", lambda: hl.candles(coin, res, start, end))
    return closed_only(r.data, res, end) if r.ok and r.data is not None else None


async def score_open_plans(s: Session, hl: Hyperliquid, now_ms: int, limit: int = 60,
                           coin: str | None = None) -> dict[str, int]:
    q = select(PlanRow).where(PlanRow.status.in_(["pending", "open"]))
    if coin is not None:
        q = q.where(PlanRow.coin == coin)
    plans = s.execute(q.order_by(PlanRow.created_ms).limit(limit)).scalars().all()
    counts = {"checked": 0, "resolved": 0, "errors": 0}
    cache: dict[tuple[str, str], pd.DataFrame] = {}
    for p in plans:
        counts["checked"] += 1
        res = resolution_for(now_ms - p.created_ms)
        key = (p.coin + str((p.inputs or {}).get("spot_series")), res)
        start = p.created_ms - 60_000
        bars = cache.get(key)
        if bars is None or (not bars.empty and int(bars.index[0].value // 1_000_000) > start):
            bars = await _bars(hl, p, start, now_ms, res)
            if bars is None:
                counts["errors"] += 1
                continue
            cache[key] = bars
        out = score(spec_of(p), bars, now_ms)
        apply_outcome(s, p, out, res)
        if out.resolved:
            counts["resolved"] += 1
    return counts


def apply_outcome(s: Session, p: PlanRow, out: Outcome, res: str) -> None:
    if out.exit_reason == "open" and not out.filled:
        return  # still pending, nothing to record
    row = s.get(PlanOutcome, p.id)
    if row is None:
        row = PlanOutcome(plan_id=p.id, filled=out.filled, exit_reason=out.exit_reason, scored_at=datetime.now(UTC))
        s.add(row)
    row.filled, row.fill_ms, row.fill_price = out.filled, out.fill_ms, out.fill_price
    row.tp1_hit, row.exit_reason, row.exit_ms, row.exit_price = out.tp1_hit, out.exit_reason, out.exit_ms, out.exit_price
    row.r_multiple, row.mfe_r, row.mae_r = out.r_multiple, out.mfe_r, out.mae_r
    row.resolution, row.scored_at = res, datetime.now(UTC)
    if out.exit_reason == "not_triggered":
        p.status = "expired"
    elif out.resolved:
        p.status = "closed"
    else:
        p.status = "open"
