"""Live auto-trading: copies the TAKE trades of the proven paper bots (breakout, trend follower) to a Hyperliquid
account, with the stop always resting on the exchange.

Each run (every few minutes, after the paper bots and the TAKE tagging):
1. Reconcile every live trade with the exchange (positions, open orders, fills): limit filled → place TP1/TP2 (the
   stop is attached to the entry and goes live with the fill); TP1 hit → stop to entry; trend trades → stop follows
   the paper bot's trailing stop; position gone → closed, P&L from the fills; a missing stop is re-placed.
2. Safety limits: account value below LIVE_FLOOR_USD or a loss of LIVE_DAILY_LOSS_USD today → no new trades.
3. If switched on (Auto page or Telegram /autostart): copy fresh TAKE trades, at most LIVE_MAX_POSITIONS, one per
   coin. live_mode=dry_run only sends "I would place…" messages.

Every order, fill, stop move, close, skip, error and pause is sent to the linked Telegram chat. Halting or stopping
never removes a stop: open positions stay protected on the exchange. The kill switch closes everything."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from perpdesk.alerts import telegram
from perpdesk.alerts.notify import _label, _px, _t, esc, half_size, safe_leverage
from perpdesk.config import Settings
from perpdesk.db.models import AutoTrade
from perpdesk.live.exchange import Account, LiveConfigError, Order, ceil_size, cloid
from perpdesk.plan import HL_MIN_NOTIONAL_USD, floor_size
from perpdesk.store import kv_get, kv_set, utcnow
from perpdesk.verdict import FRESH_MS

ON_KEY = "live:on"  # {"on": bool, "by": ..., "at": ...}: the Start/Stop switch
HALT_KEY = "live:halt"  # {"reason", "at"}: floor or kill switch; cleared by /autostart
DAY_HALT_KEY = "live:day_halt"  # UTC date the daily loss limit was hit
STATE_KEY = "live:last"  # last account snapshot, for the Auto page and /auto
ACTIVE = ("pending", "open")
FEE = 0.00045  # Hyperliquid taker fee, used for sizing
HOUR = 3_600_000


class Exchange(Protocol):
    agent: str
    account: str

    async def prepare(self) -> dict[str, Any]: ...
    async def asset(self, coin: str) -> tuple[int, int, int]: ...
    async def account_state(self) -> Account: ...
    async def open_orders(self) -> list[dict[str, Any]]: ...
    async def fills(self, start_ms: int) -> list[dict[str, Any]]: ...
    async def mids(self) -> dict[str, float]: ...
    async def place(self, orders: list[Order], grouping: str = "na") -> list[Any]: ...
    async def cancel(self, items: list[tuple[str, str]]) -> list[Any]: ...
    async def cancel_oids(self, items: list[tuple[str, int]]) -> list[Any]: ...
    async def set_leverage(self, coin: str, leverage: int, cross: bool = False) -> None: ...


@dataclass
class LiveResult:
    mode: str
    on: bool = False
    msgs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "on": self.on, "opened": self.opened, "errors": self.errors,
                "messages": len(self.msgs)}


# ---------------------------------------------------------------- switches


def is_on(s: Session) -> bool:
    v = kv_get(s, ON_KEY)
    return isinstance(v, dict) and bool(v.get("on"))


def halted(s: Session) -> dict[str, Any] | None:
    v = kv_get(s, HALT_KEY)
    return v if isinstance(v, dict) else None


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def day_halted(s: Session, now_ms: int) -> bool:
    return kv_get(s, DAY_HALT_KEY) == _day(now_ms)


def readiness(settings: Settings) -> str | None:
    """Why live mode cannot open trades, or None."""
    if settings.live_mode == "off":
        return "LIVE_MODE is off (Settings → Live auto-trading)"
    if settings.execution_mode != "paper":
        return "the paper bots are not running (EXECUTION_MODE must be paper: live copies their TAKE trades)"
    if settings.live_mode == "live":
        if not settings.auth_enabled:
            return "real-money mode needs the app password on (AUTH_ENABLED=true)"
        if not (settings.hl_agent_private_key and settings.hl_account_address):
            return "HL_AGENT_PRIVATE_KEY and HL_ACCOUNT_ADDRESS are not set in Vercel"
    return None


def start(s: Session, settings: Settings, by: str) -> str:
    why = readiness(settings)
    if why:
        return f"Not started: {why}."
    kv_set(s, ON_KEY, {"on": True, "by": by, "at": utcnow().isoformat()})
    kv_set(s, HALT_KEY, None)
    what = "REAL orders" if settings.live_mode == "live" else "DRY RUN (messages only, no orders)"
    return (f"▶️ Auto-trading started: {what}. {sizing_text(settings)}. Max {settings.live_max_positions} positions, "
            f"daily loss stop ${settings.live_daily_loss_usd:g}, floor ${settings.live_floor_usd:g}.")


async def stop(s: Session, settings: Settings, by: str, ex: Exchange | None = None) -> str:
    """No new trades; unfilled entry orders are cancelled. Open positions keep their exchange stop and targets."""
    kv_set(s, ON_KEY, {"on": False, "by": by, "at": utcnow().isoformat()})
    n = 0
    pend = _live(s, ["pending"])
    if pend and settings.live_mode == "live":
        try:
            ex = ex or _exchange(settings)
            for t in pend:
                st = t.exec_state or {}
                await ex.cancel([(t.coin, st["entry"])] + ([(t.coin, st["sl"])] if st.get("sl") else []))
                n += 1
            acct = await ex.account_state()
            for t in pend:
                if t.coin not in acct.positions:  # a partly filled one stays open (and protected); the run adopts it
                    t.status, t.exit_reason, t.exit_ms = "cancelled", "stopped", int(time.time() * 1000)
        except Exception as e:
            return f"⏸ Auto-trading stopped, but cancelling entry orders failed: {esc(e)}. Check Hyperliquid."
    opn = len(_live(s, ["open"]))
    return (f"⏸ Auto-trading stopped: no new trades. {n} unfilled entr{'y' if n == 1 else 'ies'} cancelled. "
            f"{opn} open position{'' if opn == 1 else 's'} keep{'s' if opn == 1 else ''} its stop on the exchange "
            "(/autokill closes everything).")


def sizing_text(settings: Settings) -> str:
    if settings.live_size_mode == "risk":
        return f"Size A: about ${settings.live_risk_usd:g} lost if a stop is hit"
    return f"Size B: ${settings.live_notional_usd:g} per position"


def _exchange(settings: Settings) -> Exchange:
    from perpdesk.live.exchange import HLExchange
    from perpdesk.sources.hyperliquid import Hyperliquid

    return HLExchange(settings, Hyperliquid(settings.hyperliquid_api_url, settings.hl_rate_budget_per_min))


def _live(s: Session, statuses: list[str], mode: str = "live") -> list[AutoTrade]:
    return list(s.execute(select(AutoTrade).where(AutoTrade.mode == mode, AutoTrade.status.in_(statuses))
                          .order_by(AutoTrade.created_ms)).scalars())


def realized_today(s: Session, now_ms: int) -> float:
    day0 = now_ms - now_ms % 86_400_000
    q = select(func.coalesce(func.sum(AutoTrade.pnl_usd), 0.0)).where(
        AutoTrade.mode == "live", AutoTrade.status == "closed", AutoTrade.exit_ms >= day0)
    return float(s.execute(q).scalar() or 0.0)


# ---------------------------------------------------------------- sizing (pure)


@dataclass(frozen=True)
class LiveSize:
    ok: bool
    reason: str
    qty: float = 0.0
    notional: float = 0.0
    risk_usd: float = 0.0
    leverage: int = 1
    note: str = ""


def live_size(settings: Settings, entry: float, sl: float, sz_decimals: int, asset_max_lev: int,
              half: bool = False) -> LiveSize:
    """A (risk): qty = LIVE_RISK_USD / loss per coin at the stop (fees included). B (notional): qty = LIVE_NOTIONAL_USD
    / price. A size under Hyperliquid's $10 minimum is raised to it only if that keeps the risk (A) or the size (B)
    within 1.5x of the setting; otherwise the trade is skipped. Leverage: the highest (≤ LIVE_MAX_LEVERAGE) that
    keeps the isolated liquidation at least 3x the stop distance away."""
    stop = abs(entry - sl)
    if entry <= 0 or stop <= 0:
        return LiveSize(False, "invalid stop")
    per_unit = stop + (entry + sl) * FEE
    k = 0.5 if half else 1.0
    if settings.live_size_mode == "risk":
        target, qty = settings.live_risk_usd * k, settings.live_risk_usd * k / per_unit
    else:
        target, qty = settings.live_notional_usd * k, settings.live_notional_usd * k / entry
    qty = floor_size(qty, sz_decimals)
    note = "half size (new coin)" if half else ""
    if qty * entry < HL_MIN_NOTIONAL_USD:
        need = ceil_size(HL_MIN_NOTIONAL_USD * 1.02 / entry, sz_decimals)
        over = need * per_unit if settings.live_size_mode == "risk" else need * entry
        if over > target * 1.5:
            what = f"${over:,.2f} risk" if settings.live_size_mode == "risk" else f"${need * entry:,.2f}"
            return LiveSize(False, f"the $10 minimum order would mean {what} (setting ${target:,.2f}); raise the "
                                   "amount in Settings")
        qty, note = need, (note + "; " if note else "") + "raised to the $10 minimum"
    lev = safe_leverage(stop / entry, cap=max(1, min(settings.live_max_leverage, asset_max_lev)))
    return LiveSize(True, "", qty, qty * entry, qty * per_unit, lev, note)


# ---------------------------------------------------------------- messages


def _head(t: AutoTrade, icon: str, word: str) -> str:
    return f"{icon} <b>{word} — {esc(t.coin)} {esc(t.direction)}</b> · {esc(_label(t.setup))} bot"


def _orders_text(t: AutoTrade, settings: Settings) -> str:
    side = "buy" if t.direction == "LONG" else "sell"
    if t.entry_type == "limit":
        e = (f"Limit {side} {t.qty:g} {esc(t.coin)} @ <b>{_px(t.entry)}</b> (≈ ${t.notional:,.2f}, {t.leverage}x "
             f"isolated) — cancelled at {_t(t.expires_ms, settings)} if not filled")
    else:
        e = f"Market {side} {t.qty:g} {esc(t.coin)} ≈ {_px(t.entry)} (≈ ${t.notional:,.2f}, {t.leverage}x isolated)"
    tgt = (f"TP1 {_px(t.tp1)} (half) · TP2 {_px(t.tp2)}" if t.exit_model == "fixed"
           else "no fixed target: the stop trails every 4h")
    return (f"{e}\n🛑 Stop {_px(t.sl)} (on the exchange) · 🎯 {tgt}\n"
            f"💸 Loss if stopped ≈ <b>${t.risk_usd:,.2f}</b>")


def _summary_line(s: Session, now_ms: int, acct: Account | None) -> str:
    v = f" · Account ${acct.value:,.2f}" if acct else ""
    return f"Today (UTC): {realized_today(s, now_ms):+,.2f} USD{v}"


async def _say(s: Session, settings: Settings, res: LiveResult, text: str) -> None:
    res.msgs.append(text)
    await telegram.notify(s, settings, text, html=True)


# ---------------------------------------------------------------- the run


async def run(s: Session, hl: Any, settings: Settings, now_ms: int, ex: Exchange | None = None,
              deadline: float | None = None) -> LiveResult:
    res = LiveResult(settings.live_mode, is_on(s))
    if settings.live_mode == "dry_run":
        await _dry_run(s, hl, settings, now_ms, res)
        s.commit()
    # real trades are always looked after (stops, closes), whatever the mode or switch says now
    has_trades = bool(_live(s, list(ACTIVE)))
    if not (has_trades or (settings.live_mode == "live" and res.on)):
        return res
    try:
        ex = ex or _exchange(settings)
        await ex.prepare()
        acct = await ex.account_state()
        orders = await ex.open_orders()
    except LiveConfigError as e:
        res.errors.append(str(e))
        if kv_get(s, "live:config_error") != str(e):  # say it once, not every 5 minutes
            kv_set(s, "live:config_error", str(e))
            await _say(s, settings, res, f"⚠️ <b>Live trading can't run</b>: {esc(e)}")
        return res
    except Exception as e:
        res.errors.append(f"exchange: {type(e).__name__}: {e}")
        return res
    kv_set(s, "live:config_error", None)
    by_cloid = {str(o.get("cloid")): o for o in orders if o.get("cloid")}
    for t in _live(s, list(ACTIVE)):
        try:
            await _reconcile(s, ex, settings, t, acct, by_cloid, now_ms, res, orders)
        except Exception as e:
            res.errors.append(f"{t.coin}: {type(e).__name__}: {e}")
            await _say(s, settings, res, f"⚠️ <b>LIVE ERROR — {esc(t.coin)}</b>: {esc(type(e).__name__)}: "
                                         f"{esc(str(e)[:250])}\nI'll retry next run. Check the position on Hyperliquid.")
        t.updated_at = utcnow()
        s.commit()
    acct = await ex.account_state()
    kv_set(s, STATE_KEY, {"value": acct.value, "withdrawable": acct.withdrawable, "positions": len(acct.positions),
                          "at": utcnow().isoformat(), "agent": ex.agent})
    if not res.on or settings.live_mode != "live":
        s.commit()
        return res
    if not await _limits_ok(s, settings, acct, now_ms, res):
        s.commit()
        return res
    why = readiness(settings)
    if why:
        res.errors.append(why)
        s.commit()
        return res
    for p in _candidates(s, settings, now_ms, "live"):
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            await _open_live(s, ex, settings, p, acct, now_ms, res)
        except Exception as e:
            res.errors.append(f"{p.coin}: {type(e).__name__}: {e}")
            await _say(s, settings, res, f"⚠️ <b>LIVE ERROR — {esc(p.coin)}</b> opening the trade: "
                                         f"{esc(type(e).__name__)}: {esc(str(e)[:250])}")
        s.commit()
        acct = await ex.account_state()
    return res


async def _limits_ok(s: Session, settings: Settings, acct: Account, now_ms: int, res: LiveResult) -> bool:
    h = halted(s)
    if h is None and acct.value < settings.live_floor_usd:
        reason = (f"account value ${acct.value:,.2f} is below the ${settings.live_floor_usd:g} floor. Open positions "
                  "keep their stops. Send /autostart to resume after checking.")
        kv_set(s, HALT_KEY, {"reason": reason, "at": utcnow().isoformat()})
        await _say(s, settings, res, f"🛑 <b>LIVE TRADING PAUSED</b>: {esc(reason)}")
        return False
    if h is not None:
        return False
    today = realized_today(s, now_ms)
    if today <= -settings.live_daily_loss_usd:
        if not day_halted(s, now_ms):
            kv_set(s, DAY_HALT_KEY, _day(now_ms))
            await _say(s, settings, res, f"🛑 <b>Daily loss stop</b>: {today:+,.2f} USD today (limit "
                                         f"−${settings.live_daily_loss_usd:g}). No new live trades until 00:00 UTC. "
                                         "Open positions keep their stops.")
        return False
    return True


def _candidates(s: Session, settings: Settings, now_ms: int, mode: str) -> list[AutoTrade]:
    """Fresh TAKE trades of the live bots that have not been copied yet."""
    rows = s.execute(select(AutoTrade).where(
        AutoTrade.mode == "paper", AutoTrade.take.is_(True), AutoTrade.status.in_(ACTIVE),
        AutoTrade.created_ms >= now_ms - FRESH_MS).order_by(AutoTrade.created_ms)).scalars().all()
    done = {c for (c,) in s.execute(select(AutoTrade.client_id).where(AutoTrade.mode == mode))}
    return [p for p in rows if p.setup in settings.live_bots and p.exit_model in ("fixed", "trail")
            and f"{mode}:{p.id}" not in done]


@dataclass
class EntryPlan:
    ok: bool
    reason: str = ""
    kind: str = "market"  # limit | market
    px: float = 0.0  # limit price (limit) or the worst accepted price (market)
    ref: float = 0.0  # price the size is computed from
    sl: float = 0.0


def entry_plan(p: AutoTrade, mid: float | None, settings: Settings) -> EntryPlan:
    d = 1 if p.direction == "LONG" else -1
    sl = p.stop_now if (p.exit_model == "trail" and p.stop_now) else p.sl
    if p.entry_type == "limit" and p.fill_px is None and p.status == "pending":
        if mid is not None and (mid - sl) * d <= 0:
            return EntryPlan(False, "price is already beyond the stop")
        return EntryPlan(True, kind="limit", px=p.entry, ref=p.entry, sl=sl)
    if mid is None:
        return EntryPlan(False, "no current price")
    ref = p.fill_px or p.entry
    r = abs(ref - sl)
    if r <= 0 or (mid - sl) * d <= 0:
        return EntryPlan(False, "price is already beyond the stop")
    moved = (mid - ref) * d / r
    if moved > settings.live_max_slippage_r:
        return EntryPlan(False, f"price already ran {moved:.2f}R past the entry (limit "
                                f"{settings.live_max_slippage_r:g}R)")
    cap = ref + d * settings.live_max_slippage_r * r  # never pay more than entry + 0.3R
    px = min(mid * 1.005, cap) if d == 1 else max(mid * 0.995, cap)
    return EntryPlan(True, kind="market", px=px, ref=mid, sl=sl)


def _new_live(p: AutoTrade, mode: str, ep: EntryPlan, sz: LiveSize, now_ms: int, asset: tuple[int, int, int],
              acct_tag: str) -> AutoTrade:
    tag = f"{acct_tag}:{p.id}:{now_ms}"
    st = {"paper_id": p.id, "seq": 0, "szd": asset[1], "entry": cloid(tag, "entry"), "sl": cloid(tag, "sl", 0),
          "sl_px": ep.sl, "tp1": None, "tp2": None, "tag": tag, "note": sz.note}
    return AutoTrade(
        mode=mode, coin=p.coin, asset_id=p.asset_id, setup=p.setup, direction=p.direction, status="pending",
        signal_ms=p.signal_ms, created_ms=now_ms, entry_type=ep.kind, entry=ep.ref, sl=ep.sl, tp1=p.tp1, tp2=p.tp2,
        runaway=p.runaway, expires_ms=p.expires_ms if ep.kind == "limit" else now_ms + 5 * 60_000,
        max_hold_ms=p.max_hold_ms, qty=sz.qty, leverage=sz.leverage, notional=sz.notional,
        margin=sz.notional / sz.leverage, risk_usd=sz.risk_usd, liq_price=None, entry_fee=FEE, exit_fee=FEE,
        slippage=0.0, funding_per_hour=0.0, confidence=0, client_id=f"{mode}:{p.id}", tp1_hit=False,
        note=p.note or "", exit_model=p.exit_model, atr=p.atr, trail_mult=p.trail_mult, stop_now=ep.sl, best=None,
        take=True, take_reason=p.take_reason, exec_state=st, updated_at=utcnow())


async def _skip(s: Session, settings: Settings, p: AutoTrade, mode: str, reason: str, res: LiveResult,
                now_ms: int) -> None:
    """Record the skipped copy (so it is decided once) and say why."""
    s.add(AutoTrade(
        mode=mode, coin=p.coin, asset_id=p.asset_id, setup=p.setup, direction=p.direction, status="cancelled",
        signal_ms=p.signal_ms, created_ms=now_ms, entry_type=p.entry_type, entry=p.entry, sl=p.sl, tp1=p.tp1,
        tp2=p.tp2, runaway=p.runaway, expires_ms=now_ms, max_hold_ms=0, qty=0, leverage=1, notional=0, margin=0,
        risk_usd=0, entry_fee=FEE, exit_fee=FEE, slippage=0, client_id=f"{mode}:{p.id}", exit_reason="skipped",
        exit_ms=now_ms, note=reason[:500], exit_model=p.exit_model, take=True, exec_state={"paper_id": p.id},
        updated_at=utcnow()))
    word = "DRY RUN — would skip" if mode == "dry" else "Not taken live"
    await _say(s, settings, res, f"⏭️ <b>{word} — {esc(p.coin)} {esc(p.direction)}</b> · {esc(_label(p.setup))} "
                                 f"bot TAKE\nWhy: {esc(reason)}")


async def _open_live(s: Session, ex: Exchange, settings: Settings, p: AutoTrade, acct: Account, now_ms: int,
                     res: LiveResult) -> None:
    active = _live(s, list(ACTIVE))
    if len(active) >= settings.live_max_positions:
        return await _skip(s, settings, p, "live", f"{len(active)} live positions already open (max "
                                                   f"{settings.live_max_positions})", res, now_ms)
    if any(t.coin == p.coin for t in active) or p.coin in acct.positions:
        return await _skip(s, settings, p, "live", f"already in {p.coin} on this account", res, now_ms)
    asset = await ex.asset(p.coin)
    mids = await ex.mids()
    ep = entry_plan(p, mids.get(p.coin), settings)
    if not ep.ok:
        return await _skip(s, settings, p, "live", ep.reason, res, now_ms)
    sz = live_size(settings, ep.ref, ep.sl, asset[1], asset[2], half_size(p, settings))
    if not sz.ok:
        return await _skip(s, settings, p, "live", sz.reason, res, now_ms)
    if sz.notional / sz.leverage > acct.withdrawable * 0.95:
        return await _skip(s, settings, p, "live", f"not enough free margin (${acct.withdrawable:,.2f} free, needs "
                                                   f"${sz.notional / sz.leverage:,.2f} at {sz.leverage}x)", res, now_ms)
    t = _new_live(p, "live", ep, sz, now_ms, asset, ex.account[-8:])
    # claim the signal first (client_id is unique): a second run at the same moment fails here and places nothing
    s.add(t)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        return None
    st = dict(t.exec_state or {})
    is_buy = t.direction == "LONG"
    try:
        # leftovers from an earlier trade in this coin (e.g. an untracked stop) must not hit the new position
        stale = [(o["coin"], int(o["oid"])) for o in await ex.open_orders() if o.get("coin") == t.coin and "oid" in o]
        if stale:
            await ex.cancel_oids(stale)
        await ex.set_leverage(t.coin, t.leverage, cross=False)
    except Exception as e:  # nothing was placed yet
        t.status, t.exit_reason, t.exit_ms, t.note = "cancelled", "rejected", now_ms, str(e)[:500]
        return await _say(s, settings, res, f"⚠️ <b>LIVE — {esc(t.coin)} not opened</b>: setting {t.leverage}x "
                                            f"isolated failed: {esc(str(e)[:200])}")
    stop = _stop_order(t, t.qty, st["sl"], ep.sl, asset[1])
    if ep.kind == "limit":
        # the stop is attached to the entry: it goes live the moment the entry fills, even between runs
        statuses = await ex.place([Order(t.coin, is_buy, t.qty, ep.px, st["entry"], tif="Gtc"), stop],
                                  grouping="normalTpsl")
        err = _err(statuses[0] if statuses else {"error": "no response"})
        if not err and len(statuses) > 1 and _err(statuses[1]):  # entry accepted but its stop refused: pull the entry
            await ex.cancel([(t.coin, st["entry"])])
            acct_now = await ex.account_state()
            if t.coin in acct_now.positions:  # it filled at once: protect it like a market fill
                pos = acct_now.positions[t.coin]
                return await _on_fill(s, ex, settings, t, abs(pos["szi"]), pos["entry"], now_ms, res, place_stop=True)
            err = f"the attached stop was refused ({_err(statuses[1])}), so the entry was cancelled"
        if err:
            t.status, t.exit_reason, t.exit_ms, t.note = "cancelled", "rejected", now_ms, err[:500]
            return await _say(s, settings, res, f"⚠️ <b>LIVE ORDER REJECTED — {esc(t.coin)}</b>: {esc(err)}")
        res.opened.append(f"{t.coin} {t.direction} limit {t.qty:g} @ {ep.px:g}")
        filled = (statuses[0] or {}).get("filled") if isinstance(statuses[0], dict) else None
        await _say(s, settings, res, _head(t, "🟢", "LIVE ORDER PLACED") + "\n" + _orders_text(t, settings)
                   + (f"\n({esc(sz.note)})" if sz.note else ""))
        if filled:
            await _on_fill(s, ex, settings, t, float(filled["totalSz"]), float(filled["avgPx"]), now_ms, res)
        return None
    statuses = await ex.place([Order(t.coin, is_buy, t.qty, ep.px, st["entry"], tif="Ioc")])
    stat = statuses[0] if statuses else {"error": "no response"}
    filled = stat.get("filled") if isinstance(stat, dict) else None
    if not filled or float(filled.get("totalSz", 0)) <= 0:
        t.status, t.exit_reason, t.exit_ms = "cancelled", "not_filled", now_ms
        t.note = (_err(stat) or "no fill within the price limit")[:500]
        return await _say(s, settings, res, f"⏭️ <b>LIVE — {esc(t.coin)} {esc(t.direction)} not filled</b>: "
                                            f"{esc(t.note)} (limit {_px(ep.px)}). Nothing was opened.")
    res.opened.append(f"{t.coin} {t.direction} market {filled['totalSz']} @ {filled['avgPx']}")
    await _say(s, settings, res, _head(t, "🟢", "LIVE TRADE OPENED") + "\n" + _orders_text(t, settings)
               + (f"\n({esc(sz.note)})" if sz.note else ""))
    await _on_fill(s, ex, settings, t, float(filled["totalSz"]), float(filled["avgPx"]), now_ms, res, place_stop=True)
    return None


def _err(stat: Any) -> str:
    return str(stat.get("error")) if isinstance(stat, dict) and stat.get("error") else ""


def _stop_order(t: AutoTrade, qty: float, cl: str, px: float, szd: int) -> Order:
    """Reduce-only stop-market; the limit price is 10% beyond the trigger so it fills in a fast market."""
    is_buy = t.direction != "LONG"
    worst = px * (1.1 if is_buy else 0.9)
    return Order(t.coin, is_buy, qty, worst, cl, reduce_only=True, trigger_px=px, tpsl="sl")


async def _on_fill(s: Session, ex: Exchange, settings: Settings, t: AutoTrade, qty: float, px: float, now_ms: int,
                   res: LiveResult, place_stop: bool = False) -> None:
    st = dict(t.exec_state or {})
    t.status, t.fill_px, t.fill_ms, t.qty = "open", px, now_ms, qty
    t.notional = qty * px
    t.risk_usd = qty * (abs(px - t.sl) + (px + t.sl) * FEE)
    if not place_stop:  # the stop was attached to the entry: check it really is on the exchange
        live_ids = {str(o.get("cloid")) for o in await ex.open_orders()}
        place_stop = st["sl"] not in live_ids
        if place_stop:
            st["seq"] = int(st.get("seq", 0)) + 1
            st["sl"] = cloid(st["tag"], "sl", st["seq"])
    if place_stop:
        stat = (await ex.place([_stop_order(t, qty, st["sl"], st["sl_px"], st["szd"])]))[0]
        if _err(stat):  # never leave a position without a stop: close it
            closed = (await _close_market(ex, t, qty, px))[0]
            t.exec_state = st
            if _err(closed):  # still open and unprotected: keep it open so every run retries the stop
                await _say(s, settings, res, _head(t, "🚨", "NO STOP — ACT NOW") + f"\nThe stop was rejected "
                           f"({esc(_err(stat))}) and closing at market failed too ({esc(_err(closed))}). Close "
                           f"{esc(t.coin)} on Hyperliquid now. I'll keep retrying the stop every run.")
                return
            t.status, t.exit_reason, t.exit_ms = "closed", "no_stop", now_ms
            await _say(s, settings, res, _head(t, "🚨", "CLOSED AT ONCE") + f"\nThe stop was rejected ({esc(_err(stat))}),"
                                                                           " so I closed the position at market.")
            return
    if t.exit_model == "fixed":
        await _place_targets(ex, t, st, qty)
    t.exec_state = st
    await _say(s, settings, res, _head(t, "✅", "LIVE FILLED") + f"\n{qty:g} {esc(t.coin)} @ <b>{_px(px)}</b> · stop "
                                 f"{_px(st['sl_px'])} is live on the exchange"
                                 + (f" · TP1 {_px(t.tp1)} · TP2 {_px(t.tp2)}" if t.exit_model == "fixed" else "")
                                 + f"\n💸 Loss if stopped ≈ ${t.risk_usd:,.2f}")


async def _place_targets(ex: Exchange, t: AutoTrade, st: dict[str, Any], qty: float) -> None:
    """TP1 = half, TP2 = the rest, both reduce-only take-profit orders on the exchange."""
    szd = st["szd"]
    half = floor_size(qty / 2, szd)
    rest = round(qty - half, szd)
    is_buy = t.direction != "LONG"
    orders, roles = [], []
    if half > 0 and not t.tp1_hit:
        st["tp1"] = cloid(st["tag"], "tp1")
        orders.append(Order(t.coin, is_buy, half, t.tp1, st["tp1"], reduce_only=True, trigger_px=t.tp1, tpsl="tp",
                            trigger_market=False))
        roles.append("tp1")
    st["tp2"] = cloid(st["tag"], "tp2")
    orders.append(Order(t.coin, is_buy, rest if not t.tp1_hit else qty, t.tp2, st["tp2"], reduce_only=True,
                        trigger_px=t.tp2, tpsl="tp", trigger_market=False))
    roles.append("tp2")
    statuses = await ex.place(orders)
    errs = [f"{r}: {_err(x)}" for r, x in zip(roles, statuses, strict=False) if _err(x)]
    for r, x in zip(roles, statuses, strict=False):
        if _err(x):
            st[r] = None
    st["tp_errors"] = errs


async def _close_market(ex: Exchange, t: AutoTrade, qty: float, ref_px: float) -> list[Any]:
    is_buy = t.direction != "LONG"
    mids = await ex.mids()
    px = mids.get(t.coin, ref_px) * (1.05 if is_buy else 0.95)
    tag = (t.exec_state or {}).get("tag", t.client_id)
    return await ex.place([Order(t.coin, is_buy, qty, px, cloid(tag, "close", int(time.time() * 1000)), reduce_only=True,
                                 tif="Ioc")])


async def _move_stop(ex: Exchange, t: AutoTrade, st: dict[str, Any], qty: float, new_px: float) -> str:
    """New stop first, then cancel the old one: the position is never unprotected. Returns an error or ''."""
    seq = int(st.get("seq", 0)) + 1
    new_cl = cloid(st["tag"], "sl", seq)
    stat = (await ex.place([_stop_order(t, qty, new_cl, new_px, st["szd"])]))[0]
    if _err(stat):
        return _err(stat)
    old = st.get("sl")
    st.update({"seq": seq, "sl": new_cl, "sl_px": new_px})
    if old:
        await ex.cancel([(t.coin, old)])
    t.stop_now = new_px
    return ""


async def _reconcile(s: Session, ex: Exchange, settings: Settings, t: AutoTrade, acct: Account,
                     by_cloid: dict[str, dict[str, Any]], now_ms: int, res: LiveResult,
                     orders: list[dict[str, Any]] | None = None) -> None:
    st = dict(t.exec_state or {})
    d = 1 if t.direction == "LONG" else -1
    p = acct.positions.get(t.coin)
    size = abs(p["szi"]) if p and p["szi"] * d > 0 else 0.0
    paper = s.get(AutoTrade, st.get("paper_id")) if st.get("paper_id") else None
    if t.status == "pending":
        entry_open = st.get("entry") in by_cloid
        if size > 0 and not entry_open:
            await _on_fill(s, ex, settings, t, size, p["entry"] if p else t.entry, now_ms, res)
            return
        if size > 0 and entry_open and now_ms < t.expires_ms:
            return  # partly filled; the attached stop covers the filled part
        if entry_open and (now_ms >= t.expires_ms or (paper is not None and paper.status == "cancelled")):
            await ex.cancel([(t.coin, st["entry"])])
            if size > 0:  # partly filled: keep that part, protected
                await _on_fill(s, ex, settings, t, size, p["entry"] if p else t.entry, now_ms, res)
                return
            t.status, t.exit_reason, t.exit_ms = "cancelled", "not_triggered", now_ms
            if st.get("sl"):
                await ex.cancel([(t.coin, st["sl"])])
            await _say(s, settings, res, _head(t, "⌛", "LIVE ORDER CANCELLED") + "\nThe limit entry did not fill in "
                                         "time (the setup has passed). Nothing was opened.")
            return
        if not entry_open and size == 0:  # gone without a position: filled and stopped between runs, or cancelled
            fills = [f for f in await ex.fills(t.created_ms - 5_000) if f.get("coin") == t.coin]
            if fills:
                t.fill_ms, t.fill_px = int(fills[0]["time"]), float(fills[0]["px"])
                await _closed(s, ex, settings, t, now_ms, res, fills)
            else:
                t.status, t.exit_reason, t.exit_ms = "cancelled", "gone", now_ms
                await _say(s, settings, res, _head(t, "⌛", "LIVE ORDER GONE") + "\nThe entry order is no longer on "
                                             "the exchange and nothing filled (cancelled on Hyperliquid?).")
        return
    # ---- open
    if size == 0:
        # position gone: cancel every order left in this coin (tracked or not) so none can touch a later trade
        left = [(t.coin, int(o["oid"])) for o in (orders or []) if o.get("coin") == t.coin and "oid" in o]
        if left:
            await ex.cancel_oids(left)
        await _closed(s, ex, settings, t, now_ms, res)
        return
    t.qty = t.qty or size
    # the paper bot's own kill switch never closes real positions: they keep their last stop (and targets)
    if paper is not None and paper.status in ("closed", "cancelled") and paper.exit_reason != "killed" and (
            t.exit_model == "trail" or paper.exit_reason == "timeout"):
        await _exit_now(s, ex, settings, t, st, size, f"the paper bot exited ({paper.exit_reason})", now_ms, res)
        return
    if t.max_hold_ms and t.fill_ms and now_ms - t.fill_ms > t.max_hold_ms:
        await _exit_now(s, ex, settings, t, st, size, "maximum holding time reached", now_ms, res)
        return
    # TP1 filled: half gone → stop to entry
    if t.exit_model == "fixed" and not t.tp1_hit and size <= t.qty * 0.75:
        t.tp1_hit = True
        be = t.fill_px or t.entry
        err = await _move_stop(ex, t, st, size, be)
        t.exec_state = st
        await _say(s, settings, res, _head(t, "🎯", "LIVE TP1 HIT") + f"\nHalf closed near {_px(t.tp1)}. "
                   + (f"Stop moved to entry {_px(be)}: the rest can't lose." if not err
                      else f"Moving the stop to entry FAILED ({esc(err)}); the old stop {_px(st['sl_px'])} is still live."))
        return
    # TP1 order was refused by the exchange (e.g. below its minimum): take the half at market when price gets there
    if t.exit_model == "fixed" and not t.tp1_hit and not st.get("tp1") and t.tp1:
        mid = (await ex.mids()).get(t.coin)
        if mid is not None and (mid - t.tp1) * d >= 0:
            half = floor_size(size / 2, st["szd"])
            stat = (await _close_market(ex, t, half, mid))[0] if half > 0 else {"error": "size too small"}
            if not _err(stat):
                t.qty = size  # the next run sees the smaller position and moves the stop to entry
                st["tp1_soft"] = True
                t.exec_state = st
                await _say(s, settings, res, _head(t, "🎯", "LIVE TP1 REACHED") + f"\nClosed half at market near "
                                             f"{_px(mid)}; the stop moves to entry next run.")
                return
    # trend follower: follow the paper bot's trailing stop (it only ever tightens)
    if t.exit_model == "trail" and paper is not None and paper.stop_now:
        new = paper.stop_now
        cur = float(st.get("sl_px") or t.sl)
        if (new - cur) * d > cur * 0.001:
            err = await _move_stop(ex, t, st, size, new)
            t.exec_state = st
            if err:
                await _say(s, settings, res, _head(t, "⚠️", "LIVE STOP NOT MOVED") + f"\nTrailing to {_px(new)} failed "
                                             f"({esc(err)}); the stop at {_px(cur)} is still live.")
            else:
                await _say(s, settings, res, _head(t, "↗️" if d == 1 else "↘️", "LIVE STOP TRAILED")
                           + f"\nStop {_px(cur)} → <b>{_px(new)}</b>.")
            return
    # safety net: the stop must be on the exchange and cover the whole position
    sl_order = by_cloid.get(str(st.get("sl")))
    if sl_order is None or float(sl_order.get("sz", 0) or 0) + 1e-12 < size:
        err = await _move_stop(ex, t, st, size, float(st.get("sl_px") or t.sl))
        t.exec_state = st
        await _say(s, settings, res, _head(t, "🛡", "LIVE STOP RE-PLACED") + "\nThe stop was missing or too small on "
                   "the exchange" + (f" and re-placing it FAILED: {esc(err)}. Close it manually on Hyperliquid!"
                                     if err else f"; it is back at {_px(st.get('sl_px'))} for {size:g} {esc(t.coin)}."))
    if t.exit_model == "fixed" and not st.get("tp2") and not st.get("tp_retry"):
        st["tp_retry"] = True
        await _place_targets(ex, t, st, size)
    t.exec_state = st


async def _exit_now(s: Session, ex: Exchange, settings: Settings, t: AutoTrade, st: dict[str, Any], size: float,
                    why: str, now_ms: int, res: LiveResult) -> None:
    stat = (await _close_market(ex, t, size, t.fill_px or t.entry))[0]
    if _err(stat):
        await _say(s, settings, res, _head(t, "⚠️", "LIVE EXIT FAILED") + f"\nTried to close ({esc(why)}): "
                                     f"{esc(_err(stat))}. The stop is still live; I'll retry next run.")
        return
    left = [(o["coin"], int(o["oid"])) for o in await ex.open_orders() if o.get("coin") == t.coin and "oid" in o]
    if left:
        await ex.cancel_oids(left)
    t.exit_reason = "timeout" if "time" in why else "paper_exit"
    await _closed(s, ex, settings, t, now_ms, res, why=why)


async def _closed(s: Session, ex: Exchange, settings: Settings, t: AutoTrade, now_ms: int, res: LiveResult,
                  fills: list[dict[str, Any]] | None = None, why: str = "") -> None:
    # one trade per coin on this account, so every fill of the coin since the order was sent belongs to it
    if fills is None:
        fills = [f for f in await ex.fills(t.created_ms - 5_000) if f.get("coin") == t.coin]
    fills = [f for f in fills if int(f.get("time", 0)) >= t.created_ms - 5_000]
    pnl = sum(float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0) for f in fills)
    last = fills[-1] if fills else None
    exit_px = float(last["px"]) if last else None
    st = t.exec_state or {}
    if not t.exit_reason:
        t.exit_reason = _reason(t, exit_px, float(st.get("sl_px") or t.sl))
    t.status, t.exit_ms, t.exit_px, t.pnl_usd = "closed", now_ms, exit_px, pnl
    t.r_multiple = pnl / t.risk_usd if t.risk_usd else None
    icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    acct = None
    try:
        acct = await ex.account_state()
    except Exception:
        pass
    s.flush()
    await _say(s, settings, res, _head(t, icon, "LIVE CLOSED") + f"\n{esc(why or t.exit_reason)} at {_px(exit_px)} → "
               f"<b>{pnl:+,.2f} USD</b>" + (f" ({t.r_multiple:+.2f}R)" if t.r_multiple is not None else "")
               + f"\n{_summary_line(s, now_ms, acct)}")


def _reason(t: AutoTrade, px: float | None, sl_px: float) -> str:
    if px is None:
        return "closed"
    cands = {"stop": sl_px, "tp2": t.tp2} if t.exit_model == "fixed" else {"stop": sl_px}
    if t.exit_model == "fixed" and t.tp1_hit and abs(sl_px - (t.fill_px or t.entry)) <= 0.001 * sl_px:
        cands = {"breakeven": sl_px, "tp2": t.tp2}  # the stop was moved to entry after TP1
    name, lvl = min(cands.items(), key=lambda kv: abs(px - kv[1]) if kv[1] else 1e18)
    if name == "stop" and t.exit_model == "trail" and (sl_px - t.sl) * (1 if t.direction == "LONG" else -1) > 0:
        return "trail"
    return name if lvl and abs(px - lvl) / lvl < 0.01 else "closed"


# ---------------------------------------------------------------- dry run


async def _dry_run(s: Session, hl: Any, settings: Settings, now_ms: int, res: LiveResult) -> None:
    """Same decisions as live, no orders: one "I would place…" message per TAKE trade."""
    if not res.on:
        return
    if readiness(settings):
        res.errors.append(str(readiness(settings)))
        return
    try:
        mids = await hl.all_mids()
    except Exception:
        mids = {}
    from perpdesk.db.models import Asset

    open_dry = [t for t in _live(s, list(ACTIVE), "dry")]
    for t in open_dry:  # a dry trade "ends" when its paper trade ends
        paper = s.get(AutoTrade, (t.exec_state or {}).get("paper_id"))
        if paper is None or paper.status in ("closed", "cancelled"):
            t.status, t.exit_ms, t.exit_reason = (paper.status if paper else "closed"), now_ms, "dry_run"
    for p in _candidates(s, settings, now_ms, "dry"):
        active = _live(s, list(ACTIVE), "dry")
        if len(active) >= settings.live_max_positions:
            await _skip(s, settings, p, "dry", f"{len(active)} positions already open (max "
                                               f"{settings.live_max_positions})", res, now_ms)
            continue
        if any(t.coin == p.coin for t in active):
            await _skip(s, settings, p, "dry", f"already in {p.coin}", res, now_ms)
            continue
        a = s.get(Asset, p.asset_id) if p.asset_id else None
        szd, mlev = (a.sz_decimals, a.max_leverage) if a else (2, 5)
        ep = entry_plan(p, mids.get(p.coin), settings)
        if not ep.ok:
            await _skip(s, settings, p, "dry", ep.reason, res, now_ms)
            continue
        sz = live_size(settings, ep.ref, ep.sl, szd, mlev, half_size(p, settings))
        if not sz.ok:
            await _skip(s, settings, p, "dry", sz.reason, res, now_ms)
            continue
        t = _new_live(p, "dry", ep, sz, now_ms, (0, szd, mlev), "dry")
        s.add(t)
        res.opened.append(f"dry {t.coin} {t.direction}")
        await _say(s, settings, res, _head(t, "🧪", "DRY RUN — I WOULD PLACE") + "\n" + _orders_text(t, settings)
                   + (f"\n({esc(sz.note)})" if sz.note else "") + "\nNo order was sent. Set Live mode = live in "
                   "Settings to trade for real.")


# ---------------------------------------------------------------- kill switch and status


async def kill(s: Session, settings: Settings, by: str, ex: Exchange | None = None) -> str:
    """Stop, cancel every open order on the account and close every position at market."""
    kv_set(s, ON_KEY, {"on": False, "by": by, "at": utcnow().isoformat()})
    kv_set(s, HALT_KEY, {"reason": f"kill switch ({by})", "at": utcnow().isoformat()})
    now = int(time.time() * 1000)
    for t in _live(s, list(ACTIVE), "dry"):
        t.status, t.exit_ms, t.exit_reason = "cancelled", now, "killed"
    if settings.live_mode != "live" and not _live(s, list(ACTIVE)):
        s.commit()
        return "⛔ Auto-trading killed (dry run: there were no real orders)."
    out = []
    try:
        ex = ex or _exchange(settings)
        orders = await ex.open_orders()
        if orders:
            await ex.cancel_oids([(o["coin"], int(o["oid"])) for o in orders])
        acct = await ex.account_state()
        mids = await ex.mids()
        for coin, p in acct.positions.items():
            is_buy = p["szi"] < 0
            px = mids.get(coin, p["entry"]) * (1.05 if is_buy else 0.95)
            stat = (await ex.place([Order(coin, is_buy, abs(p["szi"]), px, cloid("kill", coin, now), reduce_only=True,
                                          tif="Ioc")]))[0]
            out.append(f"{coin}: " + (f"close FAILED ({_err(stat)})" if _err(stat) else "closed"))
    except Exception as e:
        s.commit()
        return f"⛔ Auto-trading stopped, but the kill switch hit an error: {esc(e)}. Close positions on Hyperliquid!"
    for t in _live(s, ["pending"]):
        t.status, t.exit_ms, t.exit_reason = "cancelled", now, "killed"
    for t in _live(s, ["open"]):
        t.exit_reason = "killed"
        try:
            await _closed(s, ex, settings, t, now, LiveResult(settings.live_mode))
        except Exception:
            t.status = "closed"
    s.commit()
    return "⛔ <b>Kill switch</b>: all orders cancelled" + (", positions: " + esc("; ".join(out)) if out else
                                                            ", no open positions") + ". Send /autostart to begin again."


def status_text(s: Session, settings: Settings, now_ms: int) -> str:
    on = is_on(s)
    mode = {"off": "OFF", "dry_run": "DRY RUN (no orders)", "live": "LIVE (real orders)"}[settings.live_mode]
    lines = [f"<b>Auto-trading</b>: {'▶️ ON' if on else '⏸ OFF'} · mode {mode}", sizing_text(settings) + ".",
             f"Limits: max {settings.live_max_positions} positions · ≤{settings.live_max_leverage}x · daily stop "
             f"−${settings.live_daily_loss_usd:g} · floor ${settings.live_floor_usd:g}"]
    why = readiness(settings)
    if why:
        lines.append(f"⚠️ Can't trade: {esc(why)}")
    h = halted(s)
    if h:
        lines.append(f"🛑 Paused: {esc(h['reason'])}")
    if day_halted(s, now_ms):
        lines.append("🛑 Daily loss stop hit: no new trades until 00:00 UTC")
    last = kv_get(s, STATE_KEY)
    if isinstance(last, dict) and settings.live_mode == "live":
        lines.append(f"Account ${float(last.get('value', 0)):,.2f} (free ${float(last.get('withdrawable', 0)):,.2f}) · "
                     f"checked {str(last.get('at', ''))[11:16]} UTC")
    mode_db = "live" if settings.live_mode == "live" else "dry"
    rows = _live(s, list(ACTIVE), mode_db)
    for t in rows:
        st = t.exec_state or {}
        state = "waiting for fill" if t.status == "pending" else f"in @ {_px(t.fill_px)}"
        lines.append(f"• {esc(t.coin)} {esc(t.direction)} {esc(_label(t.setup))} — {state} · stop "
                     f"{_px(st.get('sl_px') or t.sl)}")
    if not rows:
        lines.append("No open live trades." if mode_db == "live" else "No open dry-run trades.")
    lines.append(f"Today (UTC): {realized_today(s, now_ms):+,.2f} USD")
    lines.append("Commands: /autostart · /autostop · /autokill · /auto")
    return "\n".join(lines)


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr else ""


def page_view(s: Session, settings: Settings, now_ms: int) -> dict[str, Any]:
    """Everything the Auto page shows about live trading (no secrets: only public addresses, shortened)."""
    from perpdesk.live.exchange import agent_address

    agent = ""
    if settings.hl_agent_private_key:
        try:
            agent = agent_address(settings.hl_agent_private_key)
        except LiveConfigError:
            agent = "invalid key"
    mode_db = "dry" if settings.live_mode == "dry_run" else "live"
    recent = list(s.execute(select(AutoTrade).where(AutoTrade.mode == mode_db)
                            .order_by(AutoTrade.created_ms.desc()).limit(15)).scalars())
    closed = [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "live", AutoTrade.status == "closed"))
              .scalars()]
    last = kv_get(s, STATE_KEY)
    return {"mode": settings.live_mode, "on": is_on(s), "why": readiness(settings), "halt": halted(s),
            "day_halt": day_halted(s, now_ms), "last": last if isinstance(last, dict) else None,
            "agent": _short(agent) if agent != "invalid key" else agent, "account": _short(settings.hl_account_address),
            "sizing": sizing_text(settings), "active": _live(s, list(ACTIVE), mode_db), "recent": recent,
            "total": sum(t.pnl_usd or 0.0 for t in closed), "n_closed": len(closed),
            "wins": sum(1 for t in closed if (t.pnl_usd or 0) > 0), "today": realized_today(s, now_ms),
            "config_error": kv_get(s, "live:config_error")}


async def check_connection(settings: Settings, ex: Exchange | None = None) -> str:
    """Read-only test: is the API wallet authorized for the account, and what does the account hold?"""
    try:
        ex = ex or _exchange(settings)
        info = await ex.prepare()
        acct = await ex.account_state()
    except Exception as e:
        return f"Connection check failed: {e}"
    kind = "sub-account" if info.get("vault") else "main account"
    return (f"OK: API wallet {_short(ex.agent)} is authorized to trade the {kind} {_short(ex.account)}. Account value "
            f"${acct.value:,.2f}, free ${acct.withdrawable:,.2f}, {len(acct.positions)} open position(s). No order "
            "was placed.")
