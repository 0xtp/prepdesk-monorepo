"""Paper trading for the strategy-lab bots, using exactly the lab's backtest code (strategies + simulators).

Directional bots (breakdown_short, trend_follow, range_revert) are evaluated once per closed 4h bar per coin.
Trades are re-simulated from their start on every tick against Hyperliquid candles (1m while young, then 5m and
15m, because Hyperliquid keeps about 5,000 candles per timeframe). Funding carry is checked hourly."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.auto import state
from perpdesk.auto.sizing import size_trade
from perpdesk.config import Settings
from perpdesk.db.models import Asset, AutoTrade
from perpdesk.lab.sim import Fees, simulate_trail
from perpdesk.lab.strategies import STRATEGIES, CarryConfig, Entry, carry_cost, carry_rolling, carry_signal
from perpdesk.pipeline import meta_of, perp_frames, plan_config
from perpdesk.plan import Costs
from perpdesk.sources.base import guarded
from perpdesk.sources.hyperliquid import Hyperliquid, closed_only
from perpdesk.store import utcnow
from perpdesk.timeutil import index_ms

ACTIVE = ("pending", "open")
H4 = 4 * 3_600_000
HOUR = 3_600_000


def resolution_for(age_ms: int) -> str:
    if age_ms <= 3 * 86_400_000:
        return "1m"
    if age_ms <= 16 * 86_400_000:
        return "5m"
    return "15m"


async def _candles(hl: Hyperliquid, coin: str, tf: str, start: int, now_ms: int) -> pd.DataFrame | None:
    r = await guarded("hyperliquid", lambda: hl.candles(coin, tf, start, now_ms))
    return closed_only(r.data, tf, now_ms) if r.ok and r.data is not None else None


async def _frames(s: Session, hl: Hyperliquid, coin: str, now_ms: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    fr = await perp_frames(s, hl, coin, ["4h", "1d"])
    return closed_only(fr["4h"], "4h", now_ms), closed_only(fr["1d"], "1d", now_ms)


async def _funding(hl: Hyperliquid, coin: str, start: int, now_ms: int) -> pd.Series | None:
    r = await guarded("hyperliquid", lambda: hl.funding_history(coin, start, now_ms))
    if not r.ok or r.data is None:
        return None
    return r.data["rate"] if not r.data.empty else pd.Series(dtype=float)


def _active(s: Session, bot: str) -> set[str]:
    return {t.coin for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.setup == bot,
                                                              AutoTrade.status.in_(ACTIVE))).scalars()}


def _close(t: AutoTrade, reason: str, exit_ms: int, exit_px: float | None, r: float | None, pnl: float,
           res: Any) -> None:
    t.status, t.exit_reason, t.exit_ms, t.exit_px = "closed", reason, exit_ms, exit_px
    t.r_multiple, t.pnl_usd = r, pnl
    res.closed.append(f"{t.setup} {t.coin} {t.direction} {reason} {pnl:+.2f} USD")


async def update(s: Session, hl: Hyperliquid, now_ms: int, res: Any) -> None:
    rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.status.in_(ACTIVE),
                                             AutoTrade.exit_model.in_(["trail", "carry"]))).scalars().all()
    for t in rows:
        try:
            if t.exit_model == "trail":
                await _update_trail(hl, t, now_ms, res)
            else:
                await _update_carry(hl, t, now_ms, res)
        except Exception as e:  # one bad trade must not stop the others
            res.errors.append(f"{t.setup} {t.coin}: {type(e).__name__}: {e}")
        t.updated_at = utcnow()


async def _update_trail(hl: Hyperliquid, t: AutoTrade, now_ms: int, res: Any) -> None:
    tf = resolution_for(now_ms - t.created_ms)
    bars = await _candles(hl, t.coin, tf, t.created_ms - 60_000, now_ms)
    if bars is None:
        res.errors.append(f"{t.setup} {t.coin}: no {tf} candles")
        return
    e = Entry(t.direction, t.sl, "trail", trail_mult=t.trail_mult, atr=t.atr,  # type: ignore[arg-type]
              max_hold_h=int(t.max_hold_ms // HOUR))
    tr = simulate_trail(e, t.created_ms, bars, now_ms, Fees(t.entry_fee, t.exit_fee, t.slippage), t.funding_per_hour)
    res.updated += 1
    if tr.filled and t.fill_ms is None:
        t.fill_ms, t.fill_px, t.status = tr.fill_ms, tr.fill_px, "open"
    t.stop_now, t.best = tr.stop_now, tr.best
    if tr.exit_reason != "open" and tr.exit_ms is not None and tr.r is not None:
        _close(t, tr.exit_reason, tr.exit_ms, tr.exit_px, tr.r, tr.r * t.risk_usd, res)


async def _update_carry(hl: Hyperliquid, t: AutoTrade, now_ms: int, res: Any) -> None:
    rates = await _funding(hl, t.coin, t.created_ms - 25 * HOUR, now_ms)
    if rates is None or rates.empty:
        res.errors.append(f"funding_carry {t.coin}: no funding history")
        return
    c = CarryConfig()
    avg = carry_rolling(rates)
    ms = index_ms(rates.to_frame())
    got = 0.0
    res.updated += 1
    for i in range(len(rates)):
        if ms[i] <= t.created_ms:
            continue
        got += float(rates.iloc[i])
        if not carry_signal(float(avg.iloc[i]), True, c):
            net = (got - carry_cost(c)) * t.notional
            _close(t, "funding_low", int(ms[i]), None, None, net, res)
            t.best = got * t.notional
            return
    t.best = got * t.notional  # funding collected so far (USD), shown on the page
    t.stop_now = None


async def open_directional(s: Session, hl: Hyperliquid, settings: Settings, bots: list[str], now_ms: int,
                           res: Any, deadline: float | None = None) -> None:
    from perpdesk.assets.registry import universe
    from perpdesk.auto.engine import _asset, drift_r, last_bar_close, out_of_time

    if not bots:
        return
    bar_close = last_bar_close(now_ms)
    active = {b: _active(s, b) for b in bots}
    btc_d1: pd.DataFrame | None = None
    marks: dict[str, float] | None = None
    coins = [c for c in settings.lab_coins if any(state.last_eval(s, f"{b}:{c}") < bar_close for b in bots)]
    for k, coin in enumerate(coins):
        if out_of_time(deadline):
            res.skipped.append(f"lab bots: time budget used, {len(coins) - k} coins left for the next run")
            return
        due = [b for b in bots if state.last_eval(s, f"{b}:{coin}") < bar_close]
        for b in due:
            state.set_last_eval(s, f"{b}:{coin}", bar_close)
        late = (now_ms - bar_close) / 60_000
        if late > settings.auto_max_entry_delay_min:
            res.skipped.append(f"{coin}: lab bots checked {late:.0f} min after the 4h close")
            s.commit()
            continue
        a = await _asset(s, hl, coin)
        if a is None:
            res.errors.append(f"{coin}: not a Hyperliquid perp")
            s.commit()
            continue
        c4h, d1 = await _frames(s, hl, coin, now_ms)
        if btc_d1 is None:
            btc_d1 = d1 if coin == "BTC" else (await _frames(s, hl, "BTC", now_ms))[1]
        if c4h.empty or int(index_ms(c4h)[-1]) + H4 != bar_close:
            res.errors.append(f"{coin}: latest 4h candle not available yet")
            s.commit()
            continue
        if marks is None:
            try:
                marks = {kk: v.mark_px for kk, v in (await universe(hl)).ctxs.items()}
            except Exception:
                marks = {}
        for b in due:
            strat = STRATEGIES[b]
            row = next(iter(strat.prepare(c4h, d1, btc_d1).iloc[[-1]].itertuples(index=False)))
            try:
                e = strat.entry_at(row)
            except (TypeError, ValueError):
                e = None
            if e is None:
                continue
            signal_px = float(getattr(row, "close"))  # noqa: B009
            px = marks.get(coin, signal_px)
            moved = drift_r(e.direction, signal_px, px, e.stop)
            d = 1 if e.direction == "LONG" else -1
            if moved > settings.auto_max_entry_drift_r or (px - e.stop) * d <= 0:
                res.skipped.append(f"{b} {coin}: {e.direction} signal, but price moved {moved:+.2f}R since the 4h "
                                   f"close (limit {settings.auto_max_entry_drift_r:g}R)")
                continue
            if coin in active[b] or len(active[b]) >= settings.auto_max_positions:
                res.skipped.append(f"{b} {coin}: {e.direction} signal, but "
                                   + ("already in this coin" if coin in active[b] else "max positions open"))
                continue
            t = _open(s, settings, a, b, e, px, bar_close, now_ms, res)
            if t is not None:
                active[b].add(coin)
        s.commit()


def _open(s: Session, settings: Settings, a: Asset, bot: str, e: Entry, px: float, bar_close: int, now_ms: int,
          res: Any) -> AutoTrade | None:
    pc = plan_config(settings, a.asset_class)
    fees = Fees(entry=(pc.taker_fee_bps + pc.slippage_bps) / 1e4, exit=pc.taker_fee_bps / 1e4,
                slippage=pc.slippage_bps / 1e4)
    c = Costs(fees.entry, fees.exit, fees.slippage, 0.0, 0.0)
    eq = state.equity(s, settings, bot=bot)
    sz = size_trade(e.direction, px, e.stop, c, eq, settings.auto_risk_pct, settings.auto_max_leverage, meta_of(a),
                    liq_buffer=pc.min_liq_buffer_atr * (e.atr or abs(px - e.stop) / 2))
    if not sz.ok:
        res.skipped.append(f"{bot} {a.hl_coin}: {e.direction} skipped, {sz.reason}")
        return None
    d = 1 if e.direction == "LONG" else -1
    t = AutoTrade(
        mode="paper", coin=a.hl_coin or a.symbol, asset_id=a.id, setup=bot, direction=e.direction, status="pending",
        signal_ms=bar_close, created_ms=now_ms, entry_type="market", entry=px, sl=e.stop, tp1=e.tp1, tp2=e.tp2,
        runaway=px + d * abs(px - e.stop) * 10, expires_ms=now_ms + HOUR, max_hold_ms=e.max_hold_h * HOUR,
        qty=sz.qty, leverage=sz.leverage, notional=sz.notional, margin=sz.margin, risk_usd=sz.risk_usd,
        liq_price=sz.liq, entry_fee=fees.entry, exit_fee=fees.exit, slippage=fees.slippage, funding_per_hour=0.0,
        confidence=0, client_id=f"paper:{bot}:{a.hl_coin}:{bar_close}", tp1_hit=False, note=e.note,
        exit_model=e.exit, atr=e.atr, trail_mult=e.trail_mult, stop_now=e.stop, best=None, updated_at=utcnow())
    s.add(t)
    res.opened.append(f"{bot} {t.coin} {e.direction} {sz.qty:g} @ {px:g} · stop {e.stop:.6g} · {sz.leverage}x")
    return t


async def open_carry(s: Session, hl: Hyperliquid, settings: Settings, now_ms: int, res: Any) -> None:
    from perpdesk.assets.registry import universe
    from perpdesk.auto.engine import _asset

    hour = now_ms - now_ms % HOUR
    if state.last_eval(s, "funding_carry") >= hour:
        return
    state.set_last_eval(s, "funding_carry", hour)
    active = _active(s, "funding_carry")
    c = CarryConfig()
    try:
        marks = {k: v.mark_px for k, v in (await universe(hl)).ctxs.items()}
    except Exception:
        marks = {}
    eq = state.equity(s, settings, bot="funding_carry")
    per_trade = eq / (c.capital_per_notional * settings.auto_max_positions)
    for coin in settings.lab_coins:
        if coin in active or len(active) >= settings.auto_max_positions:
            continue
        rates = await _funding(hl, coin, now_ms - 26 * HOUR, now_ms)
        if rates is None or len(rates) < 24:
            continue
        avg = float(carry_rolling(rates).iloc[-1])
        if not carry_signal(avg, False, c) or coin not in marks:
            continue
        a = await _asset(s, hl, coin)
        if a is None:
            continue
        px = marks[coin]
        t = AutoTrade(
            mode="paper", coin=coin, asset_id=a.id, setup="funding_carry", direction="CARRY", status="open",
            signal_ms=hour, created_ms=now_ms, entry_type="market", entry=px, sl=0.0, tp1=0.0, tp2=0.0, runaway=0.0,
            expires_ms=now_ms, max_hold_ms=0, qty=per_trade / px, leverage=2, notional=per_trade,
            margin=per_trade * c.capital_per_notional, risk_usd=0.0, liq_price=None, entry_fee=c.spot_fee,
            exit_fee=c.perp_fee, slippage=c.slippage, funding_per_hour=avg, confidence=0,
            client_id=f"paper:funding_carry:{coin}:{hour}", tp1_hit=False, fill_ms=now_ms, fill_px=px,
            note=f"24h funding {avg * 100:.4f}%/h ≈ {avg * 8766 * 100:.0f}% a year", exit_model="carry",
            updated_at=utcnow())
        s.add(t)
        active.add(coin)
        res.opened.append(f"funding_carry {coin}: short perp + long spot ${per_trade:,.0f} "
                          f"(funding ≈ {avg * 8766 * 100:.0f}%/yr)")
