"""Auto-trader switches and safety state, kept in the shared KV table."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import AutoTrade
from perpdesk.store import kv_get, kv_set

HALT = "auto:halt"  # {"reason": str, "at": iso}; cleared only by a manual resume
DAY_HALT = "auto:day_halt"  # "YYYY-MM-DD": no new entries for the rest of that UTC day
PEAK = "auto:peak:{mode}"
LAST_EVAL = "auto:eval:{coin}"


def halted(s: Session) -> dict[str, Any] | None:
    v = kv_get(s, HALT)
    return v if isinstance(v, dict) else None


def halt(s: Session, reason: str) -> None:
    kv_set(s, HALT, {"reason": reason, "at": datetime.now(UTC).isoformat()})


def resume(s: Session) -> None:
    kv_set(s, HALT, None)
    kv_set(s, DAY_HALT, None)


def day_halted(s: Session, now_ms: int) -> bool:
    return kv_get(s, DAY_HALT) == _day(now_ms)


def halt_day(s: Session, now_ms: int) -> None:
    kv_set(s, DAY_HALT, _day(now_ms))


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def day_start_ms(now_ms: int) -> int:
    return now_ms - now_ms % 86_400_000


def realized(s: Session, mode: str, since_ms: int | None = None) -> float:
    q = select(func.coalesce(func.sum(AutoTrade.pnl_usd), 0.0)).where(AutoTrade.mode == mode,
                                                                      AutoTrade.status == "closed")
    if since_ms is not None:
        q = q.where(AutoTrade.exit_ms >= since_ms)
    return float(s.execute(q).scalar() or 0.0)


def equity(s: Session, settings: Settings, mode: str = "paper") -> float:
    return settings.auto_paper_equity + realized(s, mode)


def peak(s: Session, mode: str, eq: float) -> float:
    key = PEAK.format(mode=mode)
    p = max(float(kv_get(s, key) or 0.0), eq)
    kv_set(s, key, p)
    return p


def last_eval(s: Session, coin: str) -> int:
    return int(kv_get(s, LAST_EVAL.format(coin=coin)) or 0)


def set_last_eval(s: Session, coin: str, bar_close_ms: int) -> None:
    kv_set(s, LAST_EVAL.format(coin=coin), bar_close_ms)
