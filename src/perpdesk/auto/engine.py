"""Auto-trader loop, run from the scheduled tick (paper mode).

Each run: (1) update open/pending paper trades from live 1m candles with the backtest's scorer;
(2) check safety limits (daily loss, max drawdown, manual halt); (3) once per closed 4h bar per coin,
run the normal analysis at 4h and open a paper trade when the chosen setup is a breakout (long or short),
which is the exact entry condition the backtest measured (breakout fires and agrees with the composite
direction). The 2021-2026 walk-forward backtest was positive for both directions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.analysis.types import TF_MS
from perpdesk.auto import state
from perpdesk.auto.sizing import size_trade
from perpdesk.config import Settings
from perpdesk.db.models import Asset, AutoTrade
from perpdesk.pipeline import Context, analyse_asset, meta_of, plan_config
from perpdesk.plan import Costs
from perpdesk.sources.base import guarded
from perpdesk.sources.hyperliquid import Hyperliquid, closed_only
from perpdesk.store import kv_set, utcnow
from perpdesk.tracking.scorer import PlanSpec, score

AUTO_TF = "4h"
STRATEGY = "breakout"
ACTIVE = ("pending", "open")


@dataclass
class RunResult:
    mode: str
    updated: int = 0
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    halted: str | None = None
    halts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "updated": self.updated, "opened": self.opened, "closed": self.closed,
                "skipped": self.skipped, "halted": self.halted, "halts": self.halts, "errors": self.errors}


def spec_of(t: AutoTrade) -> PlanSpec:
    return PlanSpec(direction=t.direction, entry_type=t.entry_type, entry=t.entry, sl=t.sl, tp1=t.tp1, tp2=t.tp2,
                    runaway=t.runaway, created_ms=t.created_ms, expires_ms=t.expires_ms, max_hold_ms=t.max_hold_ms,
                    entry_fee=t.entry_fee, exit_fee=t.exit_fee, slippage=t.slippage, funding_per_hour=t.funding_per_hour)


def last_bar_close(now_ms: int, tf: str = AUTO_TF) -> int:
    step = TF_MS[tf]
    return now_ms - now_ms % step


async def _bars(hl: Hyperliquid, coin: str, start: int, now_ms: int) -> pd.DataFrame | None:
    r = await guarded("hyperliquid", lambda: hl.candles(coin, "1m", start, now_ms))
    return closed_only(r.data, "1m", now_ms) if r.ok and r.data is not None else None


async def update_trades(s: Session, hl: Hyperliquid, now_ms: int, res: RunResult) -> None:
    rows = s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.status.in_(ACTIVE),
                                             AutoTrade.exit_model == "fixed")).scalars().all()
    for t in rows:
        bars = await _bars(hl, t.coin, t.created_ms - 60_000, now_ms)
        if bars is None:
            res.errors.append(f"{t.coin}: no 1m candles")
            continue
        out = score(spec_of(t), bars, now_ms)
        res.updated += 1
        if out.filled and t.fill_ms is None:
            t.fill_ms, t.fill_px, t.status = out.fill_ms, out.fill_price, "open"
        t.tp1_hit = out.tp1_hit
        if out.exit_reason == "not_triggered":
            t.status, t.exit_reason, t.exit_ms = "cancelled", "not_triggered", out.exit_ms
        elif out.resolved:
            t.status, t.exit_reason, t.exit_ms, t.exit_px = "closed", out.exit_reason, out.exit_ms, out.exit_price
            t.r_multiple = out.r_multiple
            t.pnl_usd = (out.r_multiple or 0.0) * t.risk_usd
            res.closed.append(f"{t.setup} {t.coin} {t.direction} {out.exit_reason} {t.pnl_usd:+.2f} USD")
        t.updated_at = utcnow()


def check_limits(s: Session, settings: Settings, now_ms: int, res: RunResult, bot: str = STRATEGY) -> bool:
    """True if this bot may open new trades."""
    eq = state.equity(s, settings, bot=bot)
    pk = state.peak(s, "paper", eq, bot)
    if pk > 0 and (pk - eq) / pk * 100 >= settings.auto_max_drawdown_pct and not state.halted(s, bot):
        state.halt(s, f"max drawdown: equity ${eq:,.2f} is {(pk - eq) / pk * 100:.1f}% below the ${pk:,.2f} peak "
                      f"(limit {settings.auto_max_drawdown_pct:g}%)", bot)
    today = state.realized(s, "paper", state.day_start_ms(now_ms), bot=bot)
    day_eq = eq - today
    if (today < 0 and day_eq > 0 and -today / day_eq * 100 >= settings.auto_daily_loss_pct
            and not state.day_halted(s, now_ms, bot)):
        state.halt_day(s, now_ms, bot)
    h = state.halted(s, bot)
    msg = None
    if h:
        msg = h["reason"]
    elif state.day_halted(s, now_ms, bot):
        msg = f"daily loss limit ({settings.auto_daily_loss_pct:g}%) reached; entries resume tomorrow (UTC)"
    if msg:
        res.halts[bot] = msg
        if res.halted is None:
            res.halted = msg
        return False
    return True


async def _asset(s: Session, hl: Hyperliquid, coin: str) -> Asset | None:
    a = s.execute(select(Asset).where(Asset.hl_coin == coin, Asset.perp_available.is_(True))).scalars().first()
    if a is None:
        from perpdesk.assets.registry import create_asset

        try:
            a = await create_asset(s, coin, hl)
        except Exception:
            return None
    return a


async def open_signals(s: Session, hl: Hyperliquid, ctx: Context, settings: Settings, now_ms: int, res: RunResult) -> None:
    bar_close = last_bar_close(now_ms)
    s4h = settings.model_copy(update={"trading_timeframe": AUTO_TF})
    params = {STRATEGY: {"lookback": float(settings.auto_breakout_lookback)}}
    active = {t.coin for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.setup == STRATEGY,
                                                               AutoTrade.status.in_(ACTIVE))).scalars()}
    for coin in settings.auto_coins:
        if state.last_eval(s, coin) >= bar_close:  # key kept as the bare coin for the breakout bot
            continue
        state.set_last_eval(s, coin, bar_close)
        if coin in active:
            continue
        late_min = (now_ms - bar_close) / 60_000
        if late_min > settings.auto_max_entry_delay_min:
            res.skipped.append(f"{coin}: checked {late_min:.0f} min after the 4h close (limit "
                               f"{settings.auto_max_entry_delay_min} min)")
            continue
        a = await _asset(s, hl, coin)
        if a is None:
            res.errors.append(f"{coin}: not a Hyperliquid perp")
            continue
        r = await analyse_asset(s, hl, a, ctx, s4h, save=False, log=False, setup_params=params)
        if r.error or r.plan is None or r.analysis is None:
            res.errors.append(f"{coin}: {r.error or 'no plan'}")
            continue
        p, an = r.plan, r.analysis
        if an.setup.name != STRATEGY:
            continue
        if len(active) >= settings.auto_max_positions:
            res.skipped.append(f"{coin}: breakout {p.direction}, but {len(active)} positions already open")
            continue
        pc = plan_config(settings, a.asset_class)
        c = p.costs or Costs(pc.taker_fee_bps / 1e4, pc.taker_fee_bps / 1e4, pc.slippage_bps / 1e4, 0.0, 0.0)
        eq = state.equity(s, settings, bot=STRATEGY)
        sz = size_trade(p.direction, p.entry, p.sl, c, eq, settings.auto_risk_pct, settings.auto_max_leverage,
                        meta_of(a), liq_buffer=pc.min_liq_buffer_atr * an.atr)
        if not sz.ok:
            res.skipped.append(f"{coin}: breakout {p.direction} skipped, {sz.reason}")
            continue
        t = AutoTrade(
            mode="paper", coin=coin, asset_id=a.id, setup=STRATEGY, direction=p.direction, status="pending",
            signal_ms=bar_close, created_ms=now_ms, entry_type=p.entry_type, entry=p.entry, sl=p.sl, tp1=p.tp1,
            tp2=p.tp2, runaway=p.runaway_price, expires_ms=now_ms + p.ttl_min * 60_000,
            max_hold_ms=pc.max_hold_hours * 3_600_000, qty=sz.qty, leverage=sz.leverage, notional=sz.notional,
            margin=sz.margin, risk_usd=sz.risk_usd, liq_price=sz.liq, entry_fee=c.entry_fee_rate,
            exit_fee=c.exit_fee_rate, slippage=c.slippage_rate, funding_per_hour=c.funding_rate_per_hour,
            confidence=p.confidence, client_id=f"paper:{coin}:{bar_close}", tp1_hit=False,
            note=an.setup.note or "", updated_at=utcnow())
        s.add(t)
        active.add(coin)
        res.opened.append(f"breakout {coin} {p.direction} {sz.qty:g} @ {p.entry:g} · SL {p.sl:g} · TP1 {p.tp1:g} · "
                          f"{sz.leverage}x")


async def run(s: Session, hl: Hyperliquid, ctx: Context, settings: Settings, now_ms: int) -> RunResult:
    from perpdesk.auto import lab_paper

    res = RunResult(settings.execution_mode)
    if settings.execution_mode != "paper":
        return res
    await update_trades(s, hl, now_ms, res)
    await lab_paper.update(s, hl, now_ms, res)
    s.flush()
    if check_limits(s, settings, now_ms, res, STRATEGY):
        await open_signals(s, hl, ctx, settings, now_ms, res)
    directional = [b for b in settings.lab_bots if b != "funding_carry"
                   and check_limits(s, settings, now_ms, res, b)]
    await lab_paper.open_directional(s, hl, settings, directional, now_ms, res)
    if "funding_carry" in settings.lab_bots and check_limits(s, settings, now_ms, res, "funding_carry"):
        await lab_paper.open_carry(s, hl, settings, now_ms, res)
    kv_set(s, "auto:last_run", {**res.as_dict(), "at": utcnow().isoformat()})
    return res


async def kill(s: Session, hl: Hyperliquid, now_ms: int, reason: str = "kill switch") -> list[str]:
    """Halt, cancel pending paper trades and close open ones at the current mark price."""
    from perpdesk.assets.registry import universe

    state.halt(s, reason)
    out = []
    try:
        marks = {k: v.mark_px for k, v in (await universe(hl)).ctxs.items()}
    except Exception:
        marks = {}
    for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.status.in_(ACTIVE))).scalars():
        t.exit_ms, t.updated_at = now_ms, utcnow()
        if t.status == "pending" or t.fill_px is None:
            t.status, t.exit_reason = "cancelled", "killed"
            out.append(f"{t.setup} {t.coin} cancelled")
            continue
        if t.exit_model == "carry":  # hedged: close both legs, keep the funding collected so far
            from perpdesk.lab.strategies import CarryConfig, carry_cost

            t.status, t.exit_reason = "closed", "killed"
            t.pnl_usd = (t.best or 0.0) - carry_cost(CarryConfig()) * t.notional
            out.append(f"{t.setup} {t.coin} closed ({t.pnl_usd:+.2f} USD)")
            continue
        px = marks.get(t.coin)
        if px is None:
            out.append(f"{t.setup} {t.coin}: no price, left open")
            continue
        d = 1 if t.direction == "LONG" else -1
        share = 0.5 if t.tp1_hit else 1.0
        leg = ((px - t.fill_px) * d - t.fill_px * t.entry_fee - px * (t.exit_fee + t.slippage)) * t.qty * share
        banked = 0.0
        if t.tp1_hit:
            banked = ((t.tp1 - t.fill_px) * d - t.fill_px * t.entry_fee - t.tp1 * t.exit_fee) * t.qty * 0.5
        t.status, t.exit_reason, t.exit_px = "closed", "killed", px
        t.pnl_usd = leg + banked
        t.r_multiple = t.pnl_usd / t.risk_usd if t.risk_usd else None
        out.append(f"{t.setup} {t.coin} closed at {px:g} ({t.pnl_usd:+.2f} USD)")
    return out
