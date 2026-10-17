"""Auto-trader switches and safety state, kept in the shared KV table.

Each bot (breakout, breakdown_short, trend_follow, range_revert, funding_carry) has its own paper
balance, peak, daily-loss pause and drawdown halt. The kill switch is global: it halts every bot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import AutoTrade
from perpdesk.store import kv_get, kv_set

HALT = "auto:halt"  # global (kill switch): {"reason", "at"}; cleared only by a manual resume
BOTS = ("breakout", "breakdown_short", "trend_follow", "range_revert", "funding_carry")


def _bot_key(base: str, bot: str | None) -> str:
    return base if bot is None else f"{base}:{bot}"


def halted(s: Session, bot: str | None = None) -> dict[str, Any] | None:
    """Global halt, or (with bot) that bot's own drawdown halt."""
    g = kv_get(s, HALT)
    if isinstance(g, dict):
        return g
    if bot is None:
        return None
    v = kv_get(s, _bot_key(HALT, bot))
    return v if isinstance(v, dict) else None


def halt(s: Session, reason: str, bot: str | None = None) -> None:
    kv_set(s, _bot_key(HALT, bot), {"reason": reason, "at": datetime.now(UTC).isoformat()})


def resume(s: Session) -> None:
    kv_set(s, HALT, None)
    for b in BOTS:
        kv_set(s, _bot_key(HALT, b), None)
        kv_set(s, _bot_key("auto:day_halt", b), None)


def day_halted(s: Session, now_ms: int, bot: str = "breakout") -> bool:
    return kv_get(s, _bot_key("auto:day_halt", bot)) == _day(now_ms)


def halt_day(s: Session, now_ms: int, bot: str = "breakout") -> None:
    kv_set(s, _bot_key("auto:day_halt", bot), _day(now_ms))


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def day_start_ms(now_ms: int) -> int:
    return now_ms - now_ms % 86_400_000


def realized(s: Session, mode: str, since_ms: int | None = None, bot: str | None = None) -> float:
    q = select(func.coalesce(func.sum(AutoTrade.pnl_usd), 0.0)).where(AutoTrade.mode == mode,
                                                                      AutoTrade.status == "closed")
    if since_ms is not None:
        q = q.where(AutoTrade.exit_ms >= since_ms)
    if bot is not None:
        q = q.where(AutoTrade.setup == bot)
    return float(s.execute(q).scalar() or 0.0)


def equity(s: Session, settings: Settings, mode: str = "paper", bot: str = "breakout") -> float:
    return settings.auto_paper_equity + realized(s, mode, bot=bot)


def peak(s: Session, mode: str, eq: float, bot: str = "breakout") -> float:
    key = f"auto:peak:{mode}:{bot}"
    p = max(float(kv_get(s, key) or 0.0), eq)
    kv_set(s, key, p)
    return p


def peak_value(s: Session, mode: str, bot: str) -> float:
    return float(kv_get(s, f"auto:peak:{mode}:{bot}") or 0.0)


def last_eval(s: Session, key: str) -> int:
    return int(kv_get(s, f"auto:eval:{key}") or 0)


def set_last_eval(s: Session, key: str, bar_close_ms: int) -> None:
    kv_set(s, f"auto:eval:{key}", bar_close_ms)
