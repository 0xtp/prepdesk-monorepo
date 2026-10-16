"""Data for the Auto page."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.auto import state
from perpdesk.config import Settings
from perpdesk.db.models import AutoTrade
from perpdesk.store import kv_get
from perpdesk.tracking.stats import r_stats

LIVE_MIN_PAPER_DAYS = 5
LIVE_MIN_PAPER_TRADES = 3


def build(s: Session, settings: Settings, now_ms: int) -> dict[str, Any]:
    rows = list(s.execute(select(AutoTrade).where(AutoTrade.mode == "paper").order_by(AutoTrade.created_ms.desc()))
                .scalars())
    closed = [t for t in rows if t.status == "closed"]
    active = [t for t in rows if t.status in ("pending", "open")]
    eq = state.equity(s, settings)
    pk = max(float(kv_get(s, state.PEAK.format(mode="paper")) or 0.0), eq)
    today = state.realized(s, "paper", state.day_start_ms(now_ms))
    rs = [t.r_multiple for t in sorted(closed, key=lambda x: x.exit_ms or 0) if t.r_multiple is not None]
    st = r_stats(rs).as_dict() if rs else None
    curve, run = [], settings.auto_paper_equity
    for t in sorted(closed, key=lambda x: x.exit_ms or 0):
        run += t.pnl_usd or 0.0
        curve.append({"ms": t.exit_ms, "eq": run})
    first = min((t.created_ms for t in rows), default=None)
    days = (now_ms - first) / 86_400_000 if first else 0.0
    checks = [
        ("Paper mode running", settings.execution_mode == "paper"),
        (f"At least {LIVE_MIN_PAPER_DAYS} days of paper trading ({days:.1f} so far)", days >= LIVE_MIN_PAPER_DAYS),
        (f"At least {LIVE_MIN_PAPER_TRADES} paper trades closed cleanly ({len(closed)} so far)",
         len(closed) >= LIVE_MIN_PAPER_TRADES),
        ("Every paper trade entered and exited by the rules (check the table)", None),
        ("Live executor built and enabled (needs your go-ahead)", False),
    ]
    return {
        "mode": settings.execution_mode, "halted": state.halted(s), "day_halted": state.day_halted(s, now_ms),
        "equity": eq, "start": settings.auto_paper_equity, "peak": pk, "dd_pct": (pk - eq) / pk * 100 if pk else 0.0,
        "today": today, "total": eq - settings.auto_paper_equity, "stats": st, "curve": curve,
        "active": active, "closed": closed[:100], "cancelled": sum(1 for t in rows if t.status == "cancelled"),
        "last_run": kv_get(s, "auto:last_run"), "checks": checks, "settings": settings,
    }
