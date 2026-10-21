"""Alerts after each auto-trader run: tag new paper trades TAKE / not-TAKE, send TAKE alerts, closes, bot pauses,
a scheduler watchdog and a daily summary. Every message goes only to the linked Telegram chat; nothing here
places orders. Each send is recorded (on the trade or in KV) so a message is never repeated."""

from __future__ import annotations

import html
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


def _t(ms: int, settings: Settings) -> str:
    return hhmm(ms, settings.alert_tz, settings.alert_tz_label)


def your_risk(settings: Settings) -> tuple[float, str]:
    if settings.alert_account_usd:
        return (settings.alert_account_usd * settings.alert_risk_pct / 100,
                f"{settings.alert_risk_pct:g}% of ${settings.alert_account_usd:,.0f}")
    return 10.0, "$10 risk — set your account size in Settings"


def size_line(t: AutoTrade, settings: Settings) -> str:
    r = abs((t.fill_px or t.entry) - t.sl)
    if not r:
        return ""
    risk, label = your_risk(settings)
    if half_size(t, settings):
        risk, label = risk / 2, label + ", HALVED for a new coin"
    qty = risk / r
    notional = qty * (t.fill_px or t.entry)
    lev = safe_leverage(r / (t.fill_px or t.entry))
    return (f"💰 <b>Size</b> ({esc(label)} = ${risk:,.2f}): {qty:.4g} {esc(t.coin)} ≈ ${notional:,.0f}\n"
            f"⚙️ <b>Leverage</b>: up to {lev}x isolated (margin ≈ ${notional / lev:,.0f}) — liquidation stays well "
            "beyond your stop. Leverage doesn't change your profit or loss; size does.")


def safe_leverage(stop_frac: float, cap: int = 5) -> int:
    """Highest leverage whose isolated liquidation (~1/lev minus ~1% maintenance) sits at least 3x the stop distance
    away, capped at 5x."""
    if stop_frac <= 0:
        return 1
    return max(1, min(cap, int(1 / (3 * stop_frac + 0.01))))


def is_limit(t: AutoTrade) -> bool:
    return getattr(t, "entry_type", "market") == "limit" and t.fill_px is None and t.exit_model == "fixed"


def half_size(t: AutoTrade, settings: Settings) -> bool:
    return t.setup == "trend_follow" and settings.new_trend_coin(t.coin, t.created_ms)


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def _head(t: AutoTrade, icon: str, word: str) -> str:
    return f"{icon} <b>{word} — {esc(t.coin)} {esc(t.direction)}</b>  ·  {esc(_label(t.setup))} bot"


def _link(t: AutoTrade) -> str:
    return f'🔗 <a href="https://app.hyperliquid.xyz/trade/{esc(t.coin)}">Open {esc(t.coin)} on Hyperliquid</a>'


def take_message(t: AutoTrade, settings: Settings, price_now: float | None = None,
                 room_note: str = "") -> str:
    entry = t.fill_px or t.entry
    until = t.expires_ms if is_limit(t) else t.created_ms + FRESH_MS
    lines = [_head(t, "✅", "TAKE"), f"⏰ Valid until <b>{_t(until, settings)}</b>"]
    if half_size(t, settings):
        lines.append(f"🆕 New coin for this bot — use <b>HALF</b> your usual size (until "
                     f"{esc(settings.trend_extra_half_until)})")
    now = f"   (price now {_px(price_now)})" if price_now else ""
    if t.exit_model == "carry":
        lines += ["", "📍 <b>Entry</b> (hedged, both legs together)",
                  f" • SHORT {esc(t.coin)} perp", f" • BUY the same amount of {esc(t.coin)} spot",
                  f" • Why: {esc(t.note)}", "",
                  "🚪 <b>Exit</b>: close both legs when I message you that the bot exited"]
    else:
        r = abs(entry - t.sl)
        band = settings.auto_max_entry_drift_r * r
        lines += ["", "📍 <b>Entry</b>"]
        if is_limit(t):
            side = "BUY" if t.direction == "LONG" else "SELL"
            lines += [f" • <b>LIMIT {side} at {_px(t.entry)}</b>{now}",
                      f" • Don't chase. Cancel if price hits {_px(t.runaway)} first"]
        else:
            lines += [f" • <b>MARKET at {_px(entry)}</b>{now}",
                      f" • Only while price is {_px(entry - band)}–{_px(entry + band)}"]
        lines += ["", f"🛑 <b>Stop</b>:  {_px(t.sl)}  ({_pct(entry, t.sl)})"]
        if t.exit_model == "trail":
            lines += ["🎯 <b>Exit</b>: trailing stop, no fixed target — I'll message you when the bot exits"]
        else:
            lines += ["🎯 <b>Targets</b>",
                      f" • TP1  {_px(t.tp1)} ({_pct(entry, t.tp1)}) → close 50%, move stop to entry",
                      f" • TP2  {_px(t.tp2)} ({_pct(entry, t.tp2)})"]
        sl = size_line(t, settings)
        if sl:
            lines += ["", sl]
    if room_note:
        lines.append(f"⚠️ {esc(room_note)}")
    lines += [_link(t), f"<i>{DISCLAIMER}</i>"]
    return "\n".join(lines)


def fill_message(t: AutoTrade, settings: Settings) -> str:
    return "\n".join([_head(t, "🟢", "FILLED"),
                      f"Limit filled at <b>{_px(t.fill_px)}</b> ({_t(t.fill_ms or t.created_ms, settings)})", "",
                      f"🛑 Place your stop at <b>{_px(t.sl)}</b> now if you haven't",
                      f"🎯 TP1 {_px(t.tp1)}  ·  TP2 {_px(t.tp2)}", f"<i>{DISCLAIMER}</i>"])


def tp1_message(t: AutoTrade, settings: Settings) -> str:
    return "\n".join([_head(t, "🎯", "TP1 HIT"),
                      f" • Close <b>50%</b> of your position at about {_px(t.tp1)}",
                      f" • Move your stop to <b>{_px(t.fill_px or t.entry)}</b> (entry): the rest can't lose now",
                      f" • Let the rest run to TP2 {_px(t.tp2)}", f"<i>{DISCLAIMER}</i>"])


def close_message(t: AutoTrade, settings: Settings, test_line: str = "") -> str:
    what = {"tp2": "hit TP2", "be": "stopped at entry after TP1", "sl": "hit the stop",
            "stop": "hit the stop", "trail": "exited on the trailing stop", "timeout": "closed on the time limit",
            "funding_low": "closed the carry (funding dropped)", "not_triggered": "cancelled (never filled)",
            "killed": "closed by the kill switch"}.get(t.exit_reason or "", t.exit_reason or "closed")
    if t.status == "cancelled":
        why = (f"price never came back to {_px(t.entry)}" if t.exit_reason == "not_triggered"
               and t.entry_type == "limit" else esc(what))
        return "\n".join([_head(t, "⏹", "NOT FILLED"), f"The bot did not trade: {why}.",
                          "Cancel your limit order if it's still open."])
    r = t.r_multiple
    icon, word = ("🟢", "WIN") if (r or 0) > 0 else ("🔴", "LOSS") if (r or 0) < 0 else ("⚪", "FLAT")
    risk, _ = your_risk(settings)
    lines = [_head(t, icon, f"CLOSED · {word}"), f"The bot {esc(what)}" + (f" at {_px(t.exit_px)}" if t.exit_px else "")]
    if r is not None:
        lines.append(f"📊 Result: <b>{r:+.2f}R</b>  ≈ {'+' if r >= 0 else '−'}${abs(r) * risk:,.2f} at your size")
    if t.exit_px:
        lines.append("👉 If you copied it, close yours now.")
    if test_line:
        lines.append(test_line)
    return "\n".join(lines)


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
        v = s.get(LatestView, t.asset_id) if t.asset_id else None
        note, price_now = "", None
        if v is not None:
            upd = _iso_ms(v.payload.get("updated_at"))
            if upd is not None and now_ms - upd <= 15 * 60_000:
                price_now = v.payload.get("price")
            if t.exit_model != "carry":
                room = room_check(t.direction, t.fill_px or t.entry, t.sl, t.tp1, levels_from_view(v.payload))
                if room.status in ("caution", "block"):
                    note = f"Level in the way: {room.reason} (info only)"
        msg = take_message(t, settings, price_now, note)
        if await telegram.notify(s, settings, msg, html=True):
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
        if await telegram.notify(s, settings, close_message(t, settings, test_line(s, settings)), html=True):
            t.alert_close_ms = now_ms
            sent += 1
    return sent


async def alert_followups(s: Session, settings: Settings, now_ms: int) -> int:
    """For trades you were alerted about: the limit entry filled, and TP1 was hit (take half, stop to entry)."""
    if not settings.alerts_closes:
        return 0
    rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.alert_open_ms > 0,
                                             AutoTrade.status == "open")).scalars().all()
    sent = 0
    for t in rows:
        need_fill = t.entry_type == "limit" and t.fill_ms and t.alert_fill_ms is None
        if need_fill and await telegram.notify(s, settings, fill_message(t, settings), html=True):
            t.alert_fill_ms, sent = now_ms, sent + 1
        need_tp1 = t.exit_model == "fixed" and t.tp1_hit and t.alert_tp1_ms is None
        if need_tp1 and await telegram.notify(s, settings, tp1_message(t, settings), html=True):
            t.alert_tp1_ms, sent = now_ms, sent + 1
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
    ok = await telegram.notify(s, settings, text, html=True)
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


def test_trades(s: Session, settings: Settings) -> list[AutoTrade]:
    """Closed TAKE trades since the test started (what you would have copied)."""
    start = int(datetime.fromisoformat(settings.test_start).replace(tzinfo=UTC).timestamp() * 1000)
    return [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.take.is_(True),
                                                         AutoTrade.status == "closed",
                                                         AutoTrade.created_ms >= start)).scalars()
            if t.r_multiple is not None]


def test_line(s: Session, settings: Settings) -> str:
    ts = test_trades(s, settings)
    if not ts:
        return ""
    total = sum(t.r_multiple or 0.0 for t in ts)
    return f"🧮 Test so far: {len(ts)} trade(s) · <b>{total:+.2f}R</b>"


def _r_now(t: AutoTrade, price: float | None) -> str:
    entry = t.fill_px or t.entry
    if not price or not entry or not t.sl or entry == t.sl:
        return ""
    d = 1 if t.direction == "LONG" else -1
    return f"{(price - entry) * d / abs(entry - t.sl):+.1f}R"


def your_trades_line(s: Session, settings: Settings) -> str:
    """Your own wallet trades since the test started: matched to a TAKE signal, and rule breaks."""
    if not settings.watch_wallet:
        return "👤 Your trades: add your wallet on My trades to track them"
    from perpdesk.db.models import WalletTrade

    start = int(datetime.fromisoformat(settings.test_start).replace(tzinfo=UTC).timestamp() * 1000)
    mine = s.execute(select(WalletTrade).where(WalletTrade.address == settings.watch_wallet,
                                               WalletTrade.opened_ms >= start)).scalars().all()
    takes = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.take.is_(True),
                                              AutoTrade.created_ms >= start - 3 * HOUR)).scalars().all()
    matched = no_stop = 0
    for w in mine:
        if any(a.coin == w.coin and a.direction == w.direction and 0 <= w.opened_ms - a.created_ms <= 3 * HOUR
               for a in takes):
            matched += 1
        if w.sl_first is None:
            no_stop += 1
    breaks = (len(mine) - matched) + no_stop
    return (f"👤 Your trades: {len(mine)} · matched to TAKE {matched} · rule breaks <b>{breaks}</b>"
            + (f" ({no_stop} without a stop)" if no_stop else ""))


def summary_text(s: Session, settings: Settings, now_ms: int) -> str:
    start = datetime.fromisoformat(settings.test_start).replace(tzinfo=UTC)
    day = (datetime.fromtimestamp(now_ms / 1000, UTC) - start).days + 1
    rows = [t for t in last_day(s, settings, now_ms) if t.take]
    filled = sum(1 for t in rows if t.fill_ms or t.status == "open")
    nofill = sum(1 for t in rows if t.status == "cancelled")
    closed24 = [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.take.is_(True),
                                                             AutoTrade.status == "closed",
                                                             AutoTrade.exit_ms >= now_ms - 24 * HOUR)).scalars()]
    open_now = [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.take.is_(True),
                                                             AutoTrade.status == "open")).scalars()
                if t.setup in enabled_bots(settings)]
    head = f"📊 <b>Day {day} of 28 — Test scorecard</b>" if 1 <= day <= 28 else "📊 <b>Daily scorecard</b>"
    lines = [head, "",
             f"📈 <b>Signals (24h)</b>: {len(rows)}  ·  filled {filled}  ·  not filled {nofill}",
             f"✅ <b>Closed (24h)</b>: {len(closed24)}" + (
                 f"  ·  {sum(t.r_multiple or 0 for t in closed24):+.2f}R" if closed24 else ""),
             f"💼 <b>Open</b>: {len(open_now)}"]
    for t in open_now[:8]:
        v = s.get(LatestView, t.asset_id) if t.asset_id else None
        px = v.payload.get("price") if v is not None else None
        lines.append(f"   • {esc(t.coin)} {esc(t.direction)}  {_r_now(t, px)}  (stop {_px(t.stop_now or t.sl)})")
    ts = test_trades(s, settings)
    total = sum(t.r_multiple or 0.0 for t in ts)
    avg = f" · avg {total / len(ts):+.2f}R" if ts else ""
    lines += ["", f"🧮 <b>Test so far</b>: {len(ts)} trade(s) · {total:+.2f}R{avg}",
              "   Backtest expects ≈ +0.1R per trade (with losing streaks)", "", your_trades_line(s, settings)]
    if not rows and not open_now:
        lines.append("\nNo signals today is normal: about 1–2 a day across all coins.")
    return "\n".join(lines)


async def after_auto(s: Session, hl: Any, ctx: Any, settings: Settings, now_ms: int, res: Any,
                     deadline: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        tagged = await tag_new_trades(s, hl, ctx, settings, now_ms, deadline)
        out["tagged"] = [f"{t.setup} {t.coin} {t.direction}: {'TAKE' if t.take else 'skip'}" for t in tagged]
        if telegram.chat_id(s, settings):
            out["take_alerts"] = await alert_takes(s, settings, now_ms)
            out["followups"] = await alert_followups(s, settings, now_ms)
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
