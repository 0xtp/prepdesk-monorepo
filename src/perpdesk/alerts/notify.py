"""Alerts after each auto-trader run: tag new paper trades TAKE / not-TAKE, send TAKE alerts, closes, bot pauses,
a scheduler watchdog and a daily summary. Every message goes only to the linked Telegram chat; nothing here
places orders. Each send is recorded (on the trade or in KV) so a message is never repeated."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.alerts import telegram
from perpdesk.alerts.clock import hhmm, in_quiet_hours, local
from perpdesk.config import Settings
from perpdesk.db.models import Asset, AutoTrade, JobHeartbeat, LatestView
from perpdesk.store import kv_get, kv_set
from perpdesk.verdict import FRESH_MS, entry_tag, levels_from_view, room_check

HOUR = 3_600_000
TAG_WINDOW_MS = 2 * HOUR  # trades older than this when first seen are tagged "not checked"
STALE_JOB_MIN = 20
DISCLAIMER = "Paper bot signal · not financial advice"


def enabled_bots(settings: Settings) -> set[str]:
    return {"breakout", *settings.lab_bots}


def _labels() -> dict[str, Any]:
    from perpdesk.auto.report import BOT_INFO

    return BOT_INFO


def _label(bot: str) -> str:
    return str(_labels().get(bot, {}).get("label", bot))


def _px(x: float | None) -> str:
    if x is None or not x:
        return "–"
    a = abs(x)
    d = 2 if a >= 100 else 3 if a >= 10 else 4 if a >= 1 else 6
    return f"${x:,.{d}f}"


def _pct(a: float, b: float) -> str:
    return f"{(b / a - 1) * 100:+.1f}%" if a else ""


# ---------------------------------------------------------------- tagging


async def tag_new_trades(s: Session, hl: Any, ctx: Any, settings: Settings, now_ms: int,
                         deadline: float | None = None) -> list[AutoTrade]:
    """Decide, once per trade, whether the coin card would have shown TAKE when the bot opened it. A coin card
    older than 15 min is re-analysed first (at most 3 per run) so its warnings are current."""
    from perpdesk.pipeline import analyse_asset

    rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.take.is_(None),
                                             AutoTrade.created_ms >= now_ms - 7 * 24 * HOUR)).scalars().all()
    refreshed = 0
    out = []
    for t in rows:
        if now_ms - t.created_ms > TAG_WINDOW_MS:
            t.take, t.take_reason = False, "not checked (opened before TAKE tagging ran)"
            continue
        v = s.get(LatestView, t.asset_id) if t.asset_id else None
        payload = v.payload if v else None
        upd = _iso_ms((payload or {}).get("updated_at"))
        if (upd is None or now_ms - upd > 15 * 60_000) and refreshed < 3 and t.asset_id and ctx is not None \
                and (deadline is None or time.monotonic() < deadline):
            a = s.get(Asset, t.asset_id)
            if a is not None:
                try:
                    res = await analyse_asset(s, hl, a, ctx, settings, log=True)
                    payload = res.view or payload
                    refreshed += 1
                except Exception:
                    pass
        t.take, t.take_reason = entry_tag(payload, t, now_ms, settings)
        out.append(t)
    s.flush()
    return out


def _iso_ms(x: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(x).timestamp() * 1000) if x else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- messages


def size_line(t: AutoTrade, settings: Settings) -> str:
    r = abs((t.fill_px or t.entry) - t.sl)
    if not r:
        return ""
    if settings.alert_account_usd:
        risk = settings.alert_account_usd * settings.alert_risk_pct / 100
        label = f"{settings.alert_risk_pct:g}% of ${settings.alert_account_usd:,.0f}"
    else:
        risk = 10.0
        label = "$10 (set your account size in Settings for a personal size)"
    if half_size(t, settings):
        risk, label = risk / 2, label + ", HALVED for a new coin"
    qty = risk / r
    notional = qty * (t.fill_px or t.entry)
    lev = max(1, min(int(t.leverage or 1), 10))
    return (f"Size: risk {label} = ${risk:,.2f} → {qty:.4g} {t.coin} (≈${notional:,.0f}; margin ≈${notional / lev:,.0f} "
            f"at {lev}x isolated)")


def half_size(t: AutoTrade, settings: Settings) -> bool:
    return t.setup == "trend_follow" and settings.new_trend_coin(t.coin, t.created_ms)


def take_message(t: AutoTrade, settings: Settings) -> str:
    entry = t.fill_px or t.entry
    lines = [f"✅ TAKE · {t.coin} {t.direction} · {_label(t.setup)}"]
    if half_size(t, settings):
        lines.append(f"🆕 New coin for this bot — use HALF your usual size (until {settings.trend_extra_half_until}).")
    if t.exit_model == "carry":
        lines += [f"Hedged carry: SHORT {t.coin} perp and BUY the same amount of {t.coin} spot (≈${t.notional:,.0f} "
                  "each side per bot size).",
                  f"Why: {t.note}",
                  "Exit: close both legs together when I message you that the bot exited."]
    else:
        r = abs(entry - t.sl)
        band = settings.auto_max_entry_drift_r * r
        lines += [f"Entry: {_px(entry)} (market) — only while price is {_px(entry - band)}–{_px(entry + band)}",
                  f"Stop: {_px(t.sl)} ({_pct(entry, t.sl)})"]
        if t.exit_model == "trail":
            lines.append("Exit: trailing stop, no fixed target — I'll message you when the bot exits "
                         "(current stop on the Auto page).")
        else:
            lines.append(f"TP1: {_px(t.tp1)} ({_pct(entry, t.tp1)}) take 50%, then stop to entry · "
                         f"TP2: {_px(t.tp2)} ({_pct(entry, t.tp2)})")
        sl = size_line(t, settings)
        if sl:
            lines.append(sl)
    lines.append(f"Valid until {hhmm(t.created_ms + FRESH_MS, settings.alert_tz)} — after that, skip it.")
    lines += [f"https://app.hyperliquid.xyz/trade/{t.coin}", DISCLAIMER]
    return "\n".join(lines)


def close_message(t: AutoTrade, settings: Settings) -> str:
    what = {"tp2": "hit TP2 🎯", "be": "stopped at break-even after TP1", "sl": "hit the stop",
            "stop": "hit the stop", "trail": "exited on the trailing stop", "timeout": "closed on the time limit",
            "funding_low": "closed the carry (funding dropped)", "not_triggered": "cancelled (never filled)",
            "killed": "closed by the kill switch"}.get(t.exit_reason or "", t.exit_reason or "closed")
    if t.status == "cancelled":
        head = f"⏹ {t.coin} {t.direction} · {_label(t.setup)} {what}. If you entered by hand, manage it yourself."
        return head + "\n" + DISCLAIMER
    r = f"{t.r_multiple:+.2f}R" if t.r_multiple is not None else ""
    pnl = f" (paper {t.pnl_usd:+,.2f} USD)" if t.pnl_usd is not None else ""
    icon = "🟢" if (t.r_multiple or 0) > 0 else "🔴" if (t.r_multiple or 0) < 0 else "⚪"
    body = f"{icon} {t.coin} {t.direction} · {_label(t.setup)} {what}: {r}{pnl}"
    if t.exit_px:
        body += f"\nExit {_px(t.exit_px)}. If you copied it, close yours now."
    return body + "\n" + DISCLAIMER


# ---------------------------------------------------------------- senders


async def alert_takes(s: Session, settings: Settings, now_ms: int) -> int:
    if not settings.alerts_take:
        return 0
    rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.take.is_(True),
                                             AutoTrade.alert_open_ms.is_(None),
                                             AutoTrade.status.in_(["pending", "open"]),
                                             AutoTrade.created_ms >= now_ms - FRESH_MS)).scalars().all()
    sent = 0
    for t in rows:
        if t.setup not in enabled_bots(settings):
            continue
        if in_quiet_hours(now_ms, settings.alert_tz, settings.alert_quiet_hours):
            t.alert_open_ms = -now_ms  # suppressed: never sent later, and its close is not sent either
            continue
        msg = take_message(t, settings)
        v = s.get(LatestView, t.asset_id) if t.asset_id else None
        if v is not None and t.exit_model != "carry":
            room = room_check(t.direction, t.fill_px or t.entry, t.sl, t.tp1, levels_from_view(v.payload))
            if room.status in ("caution", "block"):
                msg = msg.replace("\nhttps://", f"\nⓘ Level in the way: {room.reason} (info only)\nhttps://", 1)
        if await telegram.notify(s, settings, msg):
            t.alert_open_ms = now_ms
            sent += 1
    return sent


async def alert_closes(s: Session, settings: Settings, now_ms: int) -> int:
    if not settings.alerts_closes:
        return 0
    rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.alert_open_ms > 0,
                                             AutoTrade.alert_close_ms.is_(None),
                                             AutoTrade.status.in_(["closed", "cancelled"]))).scalars().all()
    sent = 0
    for t in rows:
        if await telegram.notify(s, settings, close_message(t, settings)):
            t.alert_close_ms = now_ms
            sent += 1
    return sent


async def alert_halts(s: Session, settings: Settings, res: Any) -> int:
    if not settings.alerts_closes or res is None:
        return 0
    halts = dict(getattr(res, "halts", {}) or {})
    if getattr(res, "halted", None):
        halts["all bots"] = res.halted
    sent = 0
    for bot, why in halts.items():
        key = f"alert:halt:{bot}"
        if kv_get(s, key) == why:
            continue
        if await telegram.notify(s, settings, f"⏸ {_label(bot)} paused: {why}\nNo new paper trades from it until it "
                                              "resumes (Auto page)."):
            kv_set(s, key, why)
            sent += 1
    return sent


async def watchdog(s: Session, settings: Settings, now_ms: int, me: str = "") -> str | None:
    """Tell you once when a scheduled job stops running, and once when it is back. Each job checks the other."""
    if not settings.alerts_health or not telegram.chat_id(s, settings):
        return None
    jobs = ["tick"] + (["auto"] if settings.execution_mode == "paper" else [])
    out = None
    for name in jobs:
        if name == me:  # the running job is obviously alive; the other job reports on it
            continue
        hb = s.get(JobHeartbeat, name)
        if hb is None:
            continue
        last = hb.last_run if hb.last_run.tzinfo else hb.last_run.replace(tzinfo=UTC)
        age_min = (now_ms - last.timestamp() * 1000) / 60_000
        key = f"alert:stale:{name}"
        was = kv_get(s, key)
        limit = STALE_JOB_MIN if name == "auto" else 40
        if age_min > limit and not was:
            if await telegram.notify(s, settings, f"⚠️ The '{name}' scheduler has not run for {age_min:.0f} min. "
                                                  "Signals and alerts may be missed. Check Supabase Cron / the Auto "
                                                  "page."):
                kv_set(s, key, True)
                out = f"{name} stale"
        elif age_min <= limit and was and await telegram.notify(s, settings,
                                                                 f"✅ The '{name}' scheduler is running again."):
            kv_set(s, key, False)
    return out


async def daily_summary(s: Session, settings: Settings, now_ms: int, force: bool = False) -> bool:
    if settings.alert_daily_hour is None and not force:
        return False
    loc = local(now_ms, settings.alert_tz)
    key = "alert:daily"
    if not force and (loc.hour != settings.alert_daily_hour or kv_get(s, key) == loc.date().isoformat()):
        return False
    text = summary_text(s, settings, now_ms)
    ok = await telegram.notify(s, settings, text)
    if ok and not force:
        kv_set(s, key, loc.date().isoformat())
    return ok


def last_day(s: Session, settings: Settings, now_ms: int) -> list[AutoTrade]:
    return [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper",
                                                         AutoTrade.created_ms >= now_ms - 24 * HOUR)
                                 .order_by(AutoTrade.created_ms.desc())).scalars()
            if t.setup in enabled_bots(settings)]


def outcome(t: AutoTrade) -> str:
    if t.status == "closed":
        return f"{t.r_multiple:+.2f}R" if t.r_multiple is not None else "closed"
    if t.status == "cancelled":
        return "no fill"
    if t.status == "pending":
        return "waiting for fill"
    return "open (TP1 hit)" if t.tp1_hit else "open"


def summary_text(s: Session, settings: Settings, now_ms: int) -> str:
    rows = last_day(s, settings, now_ms)
    takes = [t for t in rows if t.take]
    others = [t for t in rows if not t.take]
    lines = [f"📊 Daily summary · {local(now_ms, settings.alert_tz):%a %d %b}",
             f"Last 24h: {len(takes)} TAKE signal(s), {len(others)} other bot trade(s)."]
    for t in takes[:10]:
        lines.append(f"• TAKE {t.coin} {t.direction} {_label(t.setup)} at {hhmm(t.created_ms, settings.alert_tz)}: "
                     f"{outcome(t)}")
    for t in others[:6]:
        lines.append(f"• skip {t.coin} {t.direction} {_label(t.setup)}: {outcome(t)} — {t.take_reason or 'untagged'}")
    closed = [t.r_multiple for t in rows if t.status == "closed" and t.r_multiple is not None]
    if closed:
        lines.append(f"Closed in 24h: {len(closed)} trade(s), {sum(closed):+.2f}R total.")
    open_n = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper",
                                               AutoTrade.status.in_(["pending", "open"]))).scalars().all()
    lines.append(f"Open paper trades now: {sum(1 for t in open_n if t.setup in enabled_bots(settings))}.")
    if not rows:
        lines.append("No signals is normal: the bots fire about 1–3 times a day across all coins.")
    return "\n".join(lines) + "\n" + DISCLAIMER


async def after_auto(s: Session, hl: Any, ctx: Any, settings: Settings, now_ms: int, res: Any,
                     deadline: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        tagged = await tag_new_trades(s, hl, ctx, settings, now_ms, deadline)
        out["tagged"] = [f"{t.setup} {t.coin} {t.direction}: {'TAKE' if t.take else 'skip'}" for t in tagged]
        if telegram.chat_id(s, settings):
            out["take_alerts"] = await alert_takes(s, settings, now_ms)
            out["close_alerts"] = await alert_closes(s, settings, now_ms)
            out["halt_alerts"] = await alert_halts(s, settings, res)
            out["daily"] = await daily_summary(s, settings, now_ms)
        s.commit()
    except Exception as e:  # alerts must never break the auto-trader
        s.rollback()
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def next_check_ms(now_ms: int) -> int:
    """Next 4h candle close (the breakout and trend bots act within a few minutes of it)."""
    return (now_ms // (4 * HOUR) + 1) * 4 * HOUR
