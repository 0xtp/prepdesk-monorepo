"""Track Record page data: honest statistics over every logged plan and over the user's own trades."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import PlanOutcome, PlanRow, UserTrade
from perpdesk.tracking.service import calibration_status
from perpdesk.tracking.stats import r_stats


def _rows(s: Session) -> list[dict[str, Any]]:
    q = (select(PlanRow, PlanOutcome).join(PlanOutcome, PlanOutcome.plan_id == PlanRow.id)
         .where(PlanOutcome.filled.is_(True), PlanOutcome.r_multiple.is_not(None)).order_by(PlanOutcome.exit_ms))
    out = []
    for p, o in s.execute(q).all():
        out.append({"coin": p.coin, "direction": p.direction, "grade": p.grade, "setup": p.setup,
                    "conf": (p.confidence // 10) * 10, "tf": p.trading_tf, "cls": p.asset_class, "r": o.r_multiple,
                    "exit_ms": o.exit_ms or p.created_ms, "tp1": o.tp1_hit})
    return out


def _breakdown(rows: list[dict[str, Any]], key: str, min_sample: int) -> list[dict[str, Any]]:
    groups: dict[Any, list[float]] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(float(r["r"]))
    out = []
    for k, rs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        st = r_stats(rs)
        out.append({"key": k, **st.as_dict(), "warn": st.n >= min_sample and st.avg_r < 0,
                    "low_sample": st.n < min_sample})
    return out


def build(s: Session, settings: Settings) -> dict[str, Any]:
    rows = _rows(s)
    ms = settings.min_sample_for_stats
    overall = r_stats([float(r["r"]) for r in rows])
    counts = dict(s.execute(select(PlanRow.status, func.count(PlanRow.id)).group_by(PlanRow.status)).all())
    not_trig = s.execute(select(func.count(PlanOutcome.plan_id)).where(PlanOutcome.exit_reason == "not_triggered")).scalar()
    now = int(time.time() * 1000)
    last30 = [r for r in rows if r["exit_ms"] >= now - 30 * 86_400_000]
    eq, curve = 0.0, []
    for r in last30:
        eq += float(r["r"])
        curve.append({"t": int(r["exit_ms"]), "r": round(eq, 3)})
    buckets, cal_err, cal_n = calibration_status(s, settings)
    trades: list[dict[str, Any]] = []
    closed_r: list[float] = []
    for t in s.execute(select(UserTrade).order_by(UserTrade.opened_at.desc())).scalars():
        d = 1 if t.direction == "LONG" else -1
        r_mult = pnl = None
        if t.exit_price is not None:
            pnl = (t.exit_price - t.fill_price) * d * t.qty
            if t.sl and abs(t.fill_price - t.sl) > 0:
                r_mult = (t.exit_price - t.fill_price) * d / abs(t.fill_price - t.sl)
        trades.append({"t": t, "pnl": pnl, "r": r_mult})
        if r_mult is not None:
            closed_r.append(r_mult)
    recent = s.execute(select(PlanRow).order_by(PlanRow.created_ms.desc()).limit(60)).scalars().unique().all()
    return {
        "overall": overall.as_dict(),
        "low_sample": overall.n < ms,
        "counts": {"total": sum(counts.values()), **counts, "not_triggered": not_trig or 0},
        "by": {k: _breakdown(rows, k, ms) for k in ("grade", "coin", "direction", "setup", "conf", "tf")},
        "curve": curve,
        "calibration": {"buckets": buckets, "error": cal_err, "n": cal_n, "limit": settings.max_calibration_error,
                        "drift": cal_n >= ms and cal_err > settings.max_calibration_error},
        "trades": trades,
        "trades_stats": r_stats(closed_r).as_dict() if closed_r else None,
        "recent": recent,
        "min_sample": ms,
    }
