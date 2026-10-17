"""Data for the Auto page: one row per bot (leaderboard) plus all open and closed paper trades."""

from __future__ import annotations

import json
from pathlib import Path
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

BOT_INFO: dict[str, dict[str, str]] = {
    "breakout": {"label": "Breakout", "sides": "LONG + SHORT", "tf": "4h",
                 "rule": "4h close above the 20-bar high (or below the 20-bar low) on volume, with the full analysis "
                         "agreeing on the direction. Stop beyond structure; 50% at TP1, stop to break-even, rest at "
                         "TP2, 48h max."},
    "breakdown_short": {"label": "Breakdown short", "sides": "SHORT", "tf": "4h",
                        "rule": "4h close below the 20-bar low on volume while the coin and BTC are in daily "
                                "downtrends. 50% at 1.5R, stop to break-even, rest at 3R, 48h max."},
    "trend_follow": {"label": "Trend follower", "sides": "LONG + SHORT", "tf": "4h",
                     "rule": "New 55-bar high (long) or low (short) in the direction of the EMA50/EMA200 trend. "
                             "Stop 2.5 ATR, then a 3 ATR trailing stop that only moves in your favour; 30 days max."},
    "range_revert": {"label": "Range trader", "sides": "LONG + SHORT", "tf": "4h",
                     "rule": "Only when ADX < 20: buy below the lower Bollinger band with RSI < 30, sell above the "
                             "upper band with RSI > 70. Stop 1.5 ATR; 50% at the middle band, rest at the far band."},
    "funding_carry": {"label": "Funding carry", "sides": "Neutral", "tf": "1h",
                      "rule": "When the 24h average funding is high (about 13%/yr or more), short the perp and hold "
                              "the same amount of spot: price moves cancel out, you collect funding. Exit when it "
                              "drops under about 7%/yr."},
}

_EXPECT = Path(__file__).resolve().parent.parent / "lab" / "expectations.json"


def expectations() -> dict[str, Any]:
    """Backtest summary per bot (2021 onward), written from the lab/engine backtest results."""
    try:
        return dict(json.loads(_EXPECT.read_text()))
    except (OSError, ValueError):
        return {}


def _bot_row(s: Session, settings: Settings, bot: str, rows: list[AutoTrade], now_ms: int,
             enabled: bool, exp: dict[str, Any]) -> dict[str, Any]:
    closed = [t for t in rows if t.status == "closed"]
    eq = state.equity(s, settings, bot=bot)
    pk = max(state.peak_value(s, "paper", bot), eq)
    rs = [t.r_multiple for t in sorted(closed, key=lambda x: x.exit_ms or 0) if t.r_multiple is not None]
    wins = sum(1 for t in closed if (t.pnl_usd or 0) > 0)
    h = state.halted(s, bot)
    return {
        "bot": bot, **BOT_INFO[bot], "enabled": enabled, "equity": eq, "total": eq - settings.auto_paper_equity,
        "today": state.realized(s, "paper", state.day_start_ms(now_ms), bot=bot),
        "dd_pct": (pk - eq) / pk * 100 if pk else 0.0, "n_closed": len(closed),
        "win_rate": wins / len(closed) if closed else None, "avg_r": r_stats(rs).avg_r if rs else None,
        "open": sum(1 for t in rows if t.status in ("pending", "open")),
        "halted": h["reason"] if h else ("daily loss limit" if state.day_halted(s, now_ms, bot) else None),
        "expect": exp.get(bot),
    }


def build(s: Session, settings: Settings, now_ms: int) -> dict[str, Any]:
    rows = list(s.execute(select(AutoTrade).where(AutoTrade.mode == "paper").order_by(AutoTrade.created_ms.desc()))
                .scalars())
    by_bot: dict[str, list[AutoTrade]] = {b: [] for b in BOT_INFO}
    for t in rows:
        by_bot.setdefault(t.setup, []).append(t)
    exp = expectations()
    enabled = {"breakout", *settings.lab_bots}
    bots = [_bot_row(s, settings, b, by_bot.get(b, []), now_ms, b in enabled, exp) for b in BOT_INFO]
    bots.sort(key=lambda b: (not b["enabled"], -b["total"]))
    closed = [t for t in rows if t.status == "closed"]
    active = [t for t in rows if t.status in ("pending", "open")]
    total_eq = sum(b["equity"] for b in bots if b["enabled"])
    start = settings.auto_paper_equity * sum(1 for b in bots if b["enabled"])
    curve, run = [], start
    for t in sorted(closed, key=lambda x: x.exit_ms or 0):
        if t.setup in enabled:
            run += t.pnl_usd or 0.0
            curve.append({"ms": t.exit_ms, "eq": run})
    first = min((t.created_ms for t in rows), default=None)
    days = (now_ms - first) / 86_400_000 if first else 0.0
    checks = [
        ("Paper mode running", settings.execution_mode == "paper"),
        (f"At least {LIVE_MIN_PAPER_DAYS} days of paper trading ({days:.1f} so far)", days >= LIVE_MIN_PAPER_DAYS),
        (f"At least {LIVE_MIN_PAPER_TRADES} paper trades closed cleanly ({len(closed)} so far)",
         len(closed) >= LIVE_MIN_PAPER_TRADES),
        ("The best bot is positive after fees and close to its backtest", None),
        ("Live executor built and enabled (needs your go-ahead)", False),
    ]
    return {
        "mode": settings.execution_mode, "halted": state.halted(s), "bots": bots, "labels": BOT_INFO,
        "equity": total_eq, "start": start, "total": total_eq - start,
        "today": sum(b["today"] for b in bots if b["enabled"]), "curve": curve,
        "active": active, "closed": closed[:150], "cancelled": sum(1 for t in rows if t.status == "cancelled"),
        "last_run": kv_get(s, "auto:last_run"), "checks": checks, "settings": settings,
    }
