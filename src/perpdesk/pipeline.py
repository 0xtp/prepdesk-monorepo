"""Live pipeline: fetch (incrementally, cached in the DB) -> MarketSnapshot -> analyse -> plan -> view.

Used by the cron tick, the on-demand refresh buttons and the CLI. Never fabricates data: when a
source fails, the snapshot records it as stale/unavailable and the card shows the error.
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.analysis.engine import EngineConfig, analyse
from perpdesk.analysis.setups import DEFAULT_PARAMS, SETUP_LABELS
from perpdesk.analysis.types import TF_MS, Analysis, AssetMeta, Flag, Level, MarketSnapshot
from perpdesk.assets.registry import universe
from perpdesk.config import Settings
from perpdesk.db.models import Asset, LatestView, PlanOutcome, PlanRow, SetupStat
from perpdesk.plan import Plan, PlanConfig, RiskInputs, build_plan
from perpdesk.sources import calendar, coingecko, coinglass, dexscreener, feargreed, yahoo
from perpdesk.sources.base import SourceResult, guarded
from perpdesk.sources.hyperliquid import Hyperliquid, PerpUniverse, closed_only
from perpdesk.store import (
    kv_get_with_time,
    kv_set,
    last_candle_ts,
    last_funding_ts,
    load_candles,
    load_funding,
    load_oi,
    save_candles,
    save_ctx_snapshot,
    save_funding,
)
from perpdesk.timeutil import index_ms
from perpdesk.tracking import service

TF_BARS = {"1w": 260, "1d": 420, "4h": 700, "1h": 1500, "30m": 1000, "15m": 1000, "5m": 500, "1m": 500}
BTC_TFS = ("1d", "4h", "1h")


def now_ms() -> int:
    return int(time.time() * 1000)


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- config adapters


def engine_config(settings: Settings, setup_params: dict[str, dict[str, float]] | None = None,
                  score_quantiles: tuple[float, ...] = ()) -> EngineConfig:
    return EngineConfig(
        score_quantiles=score_quantiles,
        tf_weights=settings.tf_weight_map, group_weights=settings.group_weights, trading_tf=settings.trading_timeframe,
        max_atr_pctl=settings.max_atr_pctl, event_blackout_hours=settings.event_blackout_hours,
        min_liquidity_usd=settings.min_liquidity_usd, setup_params=setup_params or {},
    )


def plan_config(settings: Settings, asset_class: str) -> PlanConfig:
    slip = settings.slippage_bps_major if asset_class in ("Majors", "Large") else settings.slippage_bps_alt
    return PlanConfig(
        min_rr=settings.min_rr, min_confidence=settings.min_confidence, grade_a_size=settings.grade_a_size,
        grade_b_size=settings.grade_b_size, grade_c_size=settings.grade_c_size, caution_size=settings.caution_size,
        sl_atr_buffer=settings.sl_atr_buffer, min_sl_atr=settings.min_sl_atr,
        min_liq_buffer_atr=settings.min_liq_buffer_atr, max_leverage=settings.max_leverage,
        plan_ttl_min=settings.plan_ttl_min, max_hold_hours=settings.max_hold_hours,
        taker_fee_bps=settings.taker_fee_bps, maker_fee_bps=settings.maker_fee_bps, slippage_bps=slip,
    )


def risk_inputs(settings: Settings, margin: float | None = None, risk_usd: float | None = None,
                risk_pct: float | None = None) -> RiskInputs:
    m = margin if margin and margin > 0 else settings.default_margin_usd
    if risk_usd and risk_usd > 0:
        r = risk_usd
    elif risk_pct and risk_pct > 0:
        r = m * min(risk_pct, 10.0) / 100
    elif settings.risk_usd:
        r = settings.risk_usd
    else:
        r = m * settings.risk_pct_of_margin / 100
    return RiskInputs(m, r)


def meta_of(a: Asset) -> AssetMeta:
    return AssetMeta(coin=a.hl_coin or a.symbol, is_perp=bool(a.perp_available and a.hl_coin),
                     sz_decimals=a.sz_decimals, max_leverage=max(1, a.max_leverage),
                     margin_tiers=[(float(t[0]), int(t[1])) for t in (a.margin_tiers or [])],
                     only_isolated=a.only_isolated, asset_class=a.asset_class)


# ---------------------------------------------------------------- context (shared by all assets in a run)


@dataclass
class Context:
    universe: PerpUniverse | None
    btc: dict[str, pd.DataFrame] = field(default_factory=dict)
    fear_greed: pd.DataFrame | None = None
    cme: pd.DataFrame | None = None
    macro: dict[str, pd.DataFrame] | None = None
    events: list[Any] = field(default_factory=list)
    status: dict[str, dict[str, Any]] = field(default_factory=dict)  # source -> {ok, at, error}


def _frame_to_json(df: pd.DataFrame) -> dict[str, Any]:
    num = df.select_dtypes("number")
    return {"i": [int(x) for x in index_ms(num)], "c": {str(k): [float(v) for v in num[k]] for k in num.columns}}


def _frame_from_json(d: dict[str, Any]) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(d["i"], dtype="int64"), unit="ms", utc=True), name="ts")
    return pd.DataFrame(d["c"], index=idx)


async def _cached_frame(s: Session, key: str, max_age: timedelta, fetch: Any, name: str,
                        status: dict[str, dict[str, Any]]) -> pd.DataFrame | None:
    hit = kv_get_with_time(s, key)
    if hit is not None and utcnow() - hit[1] <= max_age:
        status[name] = {"ok": True, "at": hit[1].isoformat(), "error": None, "cached": True}
        return _frame_from_json(hit[0])
    res: SourceResult[pd.DataFrame] = await guarded(name, fetch)
    if res.ok and res.data is not None and not res.data.empty:
        kv_set(s, key, _frame_to_json(res.data))
        status[name] = {"ok": True, "at": res.fetched_at.isoformat(), "error": None}
        return res.data
    status[name] = {"ok": False, "at": res.fetched_at.isoformat(), "error": res.error or "empty response"}
    if hit is not None:  # serve the last good copy, clearly timestamped
        status[name]["stale_since"] = hit[1].isoformat()
        return _frame_from_json(hit[0])
    return None


async def load_context(s: Session, hl: Hyperliquid, settings: Settings) -> Context:
    status: dict[str, dict[str, Any]] = {}
    ures = await guarded("hyperliquid", lambda: universe(hl))
    status["hyperliquid"] = {"ok": ures.ok, "at": ures.fetched_at.isoformat(), "error": ures.error}
    ctx = Context(universe=ures.data, status=status)
    if ures.ok:  # skip candle fetches entirely when Hyperliquid is down (fail fast)
        ctx.btc = await perp_frames(s, hl, "BTC", BTC_TFS, status)
    ctx.fear_greed = await _cached_frame(s, "fng", timedelta(hours=1), lambda: feargreed.history(60), "alternative.me",
                                         status)
    if settings.enable_cme_gap:
        ctx.cme = await _cached_frame(s, "cme", timedelta(hours=3), lambda: yahoo.daily("BTC=F", "6mo"),
                                      "yahoo BTC=F", status)
    if settings.enable_macro:
        dxy = await _cached_frame(s, "dxy", timedelta(hours=6), lambda: yahoo.daily("DX-Y.NYB", "1y"), "yahoo DXY",
                                  status)
        spx = await _cached_frame(s, "spx", timedelta(hours=6), lambda: yahoo.daily("^GSPC", "1y"), "yahoo S&P 500",
                                  status)
        ctx.macro = {k: v for k, v in (("dxy", dxy), ("spx", spx)) if v is not None} or None
    if settings.enable_event_calendar:
        hit = kv_get_with_time(s, "events")
        if hit is not None and utcnow() - hit[1] <= timedelta(hours=3):
            ctx.events = _events_from_json(hit[0])
            status["forexfactory"] = {"ok": True, "at": hit[1].isoformat(), "error": None, "cached": True}
        else:
            res = await guarded("forexfactory", calendar.high_impact)
            if res.ok and res.data is not None:
                ctx.events = res.data
                kv_set(s, "events", [{"t": e.title, "at": e.at.isoformat(), "i": e.impact, "c": e.country}
                                     for e in res.data])
            elif hit is not None:
                ctx.events = _events_from_json(hit[0])
            status["forexfactory"] = {"ok": res.ok, "at": res.fetched_at.isoformat(), "error": res.error}
    return ctx


def _events_from_json(rows: list[dict[str, Any]]) -> list[Any]:
    from perpdesk.analysis.types import EventInfo

    return [EventInfo(r["t"], datetime.fromisoformat(r["at"]).astimezone(UTC), r["i"], r["c"]) for r in rows]


# ---------------------------------------------------------------- candles


async def perp_frames(s: Session, hl: Hyperliquid, coin: str, tfs: tuple[str, ...] | list[str],
                      status: dict[str, dict[str, Any]] | None = None, series_prefix: str = "hl") -> dict[str, pd.DataFrame]:
    """Closed candles per TF: DB cache + incremental fetch of new bars only."""
    out: dict[str, pd.DataFrame] = {}
    series = f"{series_prefix}:{coin}"
    nm = now_ms()
    for tf in tfs:
        step = TF_MS[tf]
        bars = TF_BARS.get(tf, 500)
        last = last_candle_ts(s, series, tf)
        start = (last + step) if last is not None else nm - bars * step
        if nm - start >= step:  # at least one new closed bar may exist
            res = await guarded("hyperliquid", functools.partial(hl.candles, coin, tf, start, nm))
            if res.ok and res.data is not None:
                save_candles(s, series, tf, closed_only(res.data, tf, nm))
            elif status is not None:
                status[f"candles {coin} {tf}"] = {"ok": False, "at": res.fetched_at.isoformat(), "error": res.error}
        out[tf] = load_candles(s, series, tf, limit=bars)
    return out


async def dex_frames(s: Session, a: Asset, tfs: list[str], status: dict[str, dict[str, Any]]) -> dict[str, pd.DataFrame]:
    """Spot-only tokens: GeckoTerminal pool OHLCV (hour/day/15m); weekly resampled from daily."""
    spec = {"15m": ("minute", 15), "1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}
    out: dict[str, pd.DataFrame] = {}
    series = f"gt:{a.chain}:{a.pair_address}"
    nm = now_ms()
    for tf in tfs:
        if tf == "1w":
            continue
        if tf not in spec:
            continue
        last = last_candle_ts(s, series, tf)
        if last is None or nm - last > 2 * TF_MS[tf]:
            tfname, agg = spec[tf]
            res = await guarded("geckoterminal", functools.partial(
                coingecko.pool_ohlcv, a.chain or "", a.pair_address or "", tfname, agg))
            if res.ok and res.data is not None:
                save_candles(s, series, tf, closed_only(res.data, tf, nm))
            else:
                status[f"candles {a.symbol} {tf}"] = {"ok": False, "at": res.fetched_at.isoformat(), "error": res.error}
        out[tf] = load_candles(s, series, tf, limit=TF_BARS.get(tf, 500))
    if "1w" in tfs and "1d" in out and not out["1d"].empty:
        d = out["1d"]
        out["1w"] = d.resample(pd.Timedelta(days=7), origin="epoch", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().iloc[:-1]
    return out


async def funding_frame(s: Session, hl: Hyperliquid, coin: str, days: int = 30) -> pd.DataFrame:
    since = now_ms() - days * 86_400_000
    last = last_funding_ts(s, coin)
    start = max(since, (last + 1) if last else since)
    if now_ms() - start > 3_600_000:
        res = await guarded("hyperliquid", lambda: hl.funding_history(coin, start))
        if res.ok and res.data is not None:
            save_funding(s, coin, res.data)
    return load_funding(s, coin, since)


# ---------------------------------------------------------------- per-asset analysis


@dataclass
class AssetResult:
    asset: Asset
    analysis: Analysis | None
    plan: Plan | None
    view: dict[str, Any]
    error: str | None = None


def setup_params_for(s: Session, coin: str, cls: str) -> dict[str, dict[str, float]]:
    run = service.latest_run_id(s)
    if run is None:
        return {}
    out: dict[str, dict[str, float]] = {}
    rows = s.execute(select(SetupStat).where(SetupStat.run_id == run, SetupStat.setup != "composite")).scalars().all()
    for setup in DEFAULT_PARAMS:
        for kind, scope in (("asset", coin), ("class", cls), ("all", "*")):
            r = next((x for x in rows if x.setup == setup and x.scope_kind == kind and x.scope == scope and x.params),
                     None)
            if r is not None:
                out[setup] = {k: float(v) for k, v in r.params.items()}
                break
    return out


async def analyse_asset(s: Session, hl: Hyperliquid, a: Asset, ctx: Context, settings: Settings,
                        margin: float | None = None, risk_usd: float | None = None, risk_pct: float | None = None,
                        log: bool = False, save: bool = True) -> AssetResult:
    status = dict(ctx.status)
    meta = meta_of(a)
    tfs = list(settings.timeframes)
    if settings.trading_timeframe not in tfs:
        tfs.append(settings.trading_timeframe)
    price: float | None = None
    funding = oi = None
    cur_funding = premium = None
    day_vol = liq_usd = None
    change24 = None
    oi_now = None
    liq_levels: list[Level] = []
    try:
        if meta.is_perp:
            u = ctx.universe
            coin = a.hl_coin or a.symbol
            c = u.ctxs.get(coin) if u else None
            if c is None:
                raise RuntimeError("Hyperliquid unavailable: " + str(ctx.status.get("hyperliquid", {}).get("error")))
            price, cur_funding, premium, day_vol = c.mark_px, c.funding, c.premium, c.day_ntl_vlm
            oi_now = c.open_interest
            change24 = (c.mark_px / c.prev_day_px - 1) * 100 if c.prev_day_px else None
            save_ctx_snapshot(s, coin, now_ms(), c.mark_px, c.oracle_px, c.funding, c.open_interest, c.premium,
                              c.day_ntl_vlm)
            frames = ctx.btc if coin == "BTC" else {}
            frames = {**frames, **(await perp_frames(s, hl, coin, [t for t in tfs if t not in frames], status))}
            funding = await funding_frame(s, hl, coin)
            oi = load_oi(s, coin, now_ms() - 2 * 86_400_000)
            if settings.coinglass_api_key:
                res = await guarded("coinglass", lambda: coinglass.liquidation_levels(coin, settings.coinglass_api_key,
                                                                                       c.mark_px),
                                    cache_key=f"cg-liq:{coin}", ttl=600)
                status["coinglass"] = {"ok": res.ok, "at": res.fetched_at.isoformat(), "error": res.error}
                liq_levels = res.data or []
        elif a.hl_spot_coin:
            sp_coin = a.hl_spot_coin
            frames = await perp_frames(s, hl, sp_coin, tfs, status, series_prefix="hlspot")
            from perpdesk.assets.registry import spot_universe

            sp = next((v for v in (await spot_universe(hl)).values() if v["coin"] == sp_coin), None)
            if sp is None:
                raise RuntimeError("Hyperliquid spot price unavailable")
            price, day_vol = sp["mark_px"], sp["day_ntl_vlm"]
            change24 = (sp["mark_px"] / sp["prev_day_px"] - 1) * 100 if sp.get("prev_day_px") else None
        else:
            pairs = await dexscreener.token_pairs(a.contract or "")
            pairs = [p for p in pairs if p.pair_address == a.pair_address] or pairs
            if not pairs or pairs[0].price_usd is None:
                raise RuntimeError("DexScreener price unavailable")
            p = pairs[0]
            status["dexscreener"] = {"ok": True, "at": utcnow().isoformat(), "error": None}
            price, day_vol, liq_usd, change24 = p.price_usd, p.volume_h24, p.liquidity_usd, p.price_change_h24
            frames = await dex_frames(s, a, tfs, status)
        assert price is not None
        tr = frames.get(settings.trading_timeframe)
        stale: list[str] = []
        for tf, df in frames.items():
            if df is None or df.empty:
                stale.append(f"{tf} candles missing")
                continue
            age = now_ms() - (int(df.index[-1].value // 1_000_000) + TF_MS[tf])
            if age > 2 * TF_MS[tf] + 120_000:
                stale.append(f"{tf} candles {age // 60_000} min old")
        if tr is None or len(tr) < 60:
            raise RuntimeError(f"not enough {settings.trading_timeframe} candles ({0 if tr is None else len(tr)})")
        snap = MarketSnapshot(
            coin=a.hl_coin or a.symbol, asof=pd.Timestamp(utcnow()), price=float(price), meta=meta,
            candles={k: v for k, v in frames.items() if v is not None and not v.empty},
            btc_candles=None if (a.hl_coin == "BTC") else (ctx.btc or None),
            funding=funding if funding is not None and not funding.empty else None, oi=oi,
            current_funding=cur_funding, current_premium=premium, open_interest=oi_now, day_volume_usd=day_vol,
            liquidity_usd=liq_usd, fear_greed=ctx.fear_greed, cme_daily=ctx.cme if a.hl_coin in ("BTC", "ETH", "SOL")
            else None, macro=ctx.macro, events=ctx.events, liquidation_levels=liq_levels, stale_sources=stale,
        )
        an = analyse(snap, engine_config(settings, setup_params_for(s, snap.coin, a.asset_class),
                                         service.latest_score_quantiles(s)))
        gate = service.gate_info(s, settings, an.setup.name, snap.coin, a.asset_class, an.direction, an.confidence)
        extra: list[Flag] = []
        cf = service.calibration_flag(s, settings)
        if cf:
            extra.append(cf)
        plan = build_plan(an, meta, risk_inputs(settings, margin, risk_usd, risk_pct), plan_config(settings, a.asset_class),
                          gate, current_funding=cur_funding or 0.0, extra_flags=extra)
        hint = service.take_hint(s, settings, plan.grade)
        warnings = service.live_warnings(s, settings, snap.coin, an.confidence)
        view = build_view(a, an, plan, settings, status, change24, cur_funding, hint, warnings, frames)
        if log:
            maybe_log_plan(s, a, plan, an, settings)
        if save:
            _save_view(s, a, view, None)
        return AssetResult(a, an, plan, view)
    except Exception as e:
        view = {"asset": asset_header(a), "error": f"{type(e).__name__}: {e}", "price": price, "change24": change24,
                "status": status, "updated_at": utcnow().isoformat()}
        if save:
            _save_view(s, a, view, view["error"])
        return AssetResult(a, None, None, view, view["error"])


def _save_view(s: Session, a: Asset, view: dict[str, Any], error: str | None) -> None:
    row = s.get(LatestView, a.id)
    if row is None:
        s.add(LatestView(asset_id=a.id, payload=view, error=error, updated_at=utcnow()))
    else:
        row.payload, row.error, row.updated_at = view, error, utcnow()


def asset_header(a: Asset) -> dict[str, Any]:
    return {"id": a.id, "key": a.key, "symbol": a.symbol, "name": a.name, "perp": bool(a.perp_available and a.hl_coin),
            "icon": a.icon_url, "class": a.asset_class, "chain": a.chain, "contract": a.contract}


def build_view(a: Asset, an: Analysis, p: Plan, settings: Settings, status: dict[str, Any], change24: float | None,
               funding: float | None, hint: service.TakeHint, warnings: list[str],
               frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    tr = frames.get(settings.trading_timeframe)
    spark = [float(x) for x in tr["close"].iloc[-48:]] if tr is not None and len(tr) else []
    return {
        "asset": asset_header(a),
        "price": an.price,
        "change24": change24,
        "updated_at": utcnow().isoformat(),
        "asof": an.asof.isoformat(),
        "funding": funding,
        "funding_8h": funding * 8 if funding is not None else None,
        "funding_apr": funding * 24 * 365 if funding is not None else None,
        "direction": p.direction,
        "grade": p.grade,
        "flags": [{"code": f.code, "reason": f.reason} for f in p.flags],
        "expected_move": an.expected_move,
        "confidence": an.confidence,
        "score": an.score,
        "setup": p.setup,
        "setup_label": SETUP_LABELS.get(p.setup, p.setup),
        "setup_note": an.setup.note,
        "take": hint.take,
        "take_reason": hint.reason,
        "warnings": warnings,
        "spark": spark,
        "plan": plan_dict(p),
        "factors": [{"name": f.name, "group": f.group, "tf": f.tf, "score": f.score, "reason": f.reason,
                     "source": f.source, "scored": f.scored} for f in an.factors],
        "group_scores": an.group_scores,
        "top_for": [f"{f.name} ({f.tf}): {f.reason}" for f in an.top_for],
        "top_against": [f"{f.name} ({f.tf}): {f.reason}" for f in an.top_against],
        "levels": sorted({(round(lv.price, 10), lv.kind, lv.label) for lv in an.levels if lv.kind != "ema"},
                         key=lambda x: -x[0]),
        "range": {"high": an.range.high, "low": an.range.low} if an.range else None,
        "cme_gaps": [{"lo": g.gap_low, "hi": g.gap_high, "opened": g.opened.isoformat(), "filled": g.filled,
                      "dist": g.distance_pct} for g in an.cme_gaps],
        "atr": an.atr,
        "atr_pctl": an.atr_pctl,
        "notes": p.notes,
        "status": status,
        "trading_tf": an.trading_tf,
    }


def plan_dict(p: Plan) -> dict[str, Any]:
    return {
        "direction": p.direction, "grade": p.grade, "entry_type": p.entry_type, "entry": p.entry,
        "entry_market": p.entry_market, "zone": list(p.entry_zone), "sl": p.sl, "tp1": p.tp1, "tp2": p.tp2,
        "split": list(p.tp_split), "rr_tp1": p.rr_tp1, "rr_tp2": p.rr_tp2, "risk_usd": p.risk_usd,
        "risk_target": p.risk_usd_target, "notional": p.notional, "qty": p.qty, "leverage": p.leverage,
        "margin_used": p.margin_used, "liq": p.liq_price, "liq_buffer_atr": p.liq_buffer_atr, "fees": p.fees_usd,
        "funding_usd": p.funding_usd, "usd_tp1": p.usd_tp1, "usd_tp2": p.usd_tp2_total, "invalidation": p.invalidation,
        "entry_note": p.entry_note, "ttl_min": p.ttl_min, "created_ms": p.created_at_ms, "expires_ms": p.expires_at_ms,
        "spot_only": p.spot_only, "break_even": p.break_even, "ev_usd": p.ev_usd, "ev_basis": p.ev_basis,
        "win": None if p.win is None else {"p": p.win.p, "lo": p.win.lo, "hi": p.win.hi, "n": p.win.n,
                                           "scope": p.win.scope, "low_sample": p.win.low_sample},
        "proof": p.proof, "live_proof": p.live_proof, "copy": p.copy_text(), "setup_label": p.setup_label,
    }


# ---------------------------------------------------------------- plan logging


def maybe_log_plan(s: Session, a: Asset, p: Plan, an: Analysis, settings: Settings) -> PlanRow | None:
    """One active plan per asset. A pending (unfilled) plan is superseded if the direction flips."""
    active = s.execute(select(PlanRow).where(PlanRow.coin == an.coin, PlanRow.status.in_(["pending", "open"]))
                       .order_by(PlanRow.created_ms.desc())).scalars().first()
    if active is not None:
        if active.status == "pending" and active.direction != p.direction:
            active.status = "expired"
            s.add(PlanOutcome(plan_id=active.id, filled=False, exit_reason="not_triggered", exit_ms=p.created_at_ms,
                              scored_at=utcnow(), resolution="-"))
        else:
            return None
    pc = plan_config(settings, a.asset_class)
    row = PlanRow(
        asset_id=a.id, coin=an.coin, asset_class=a.asset_class, created_ms=p.created_at_ms, expires_ms=p.expires_at_ms,
        trading_tf=an.trading_tf, direction=p.direction, grade=p.grade, setup=p.setup, setup_params=an.setup.params,
        confidence=an.confidence, expected_move=an.expected_move, spot_only=p.spot_only, entry_type=p.entry_type,
        entry=p.entry, entry_market=p.entry_market, zone_lo=p.entry_zone[0], zone_hi=p.entry_zone[1], sl=p.sl,
        tp1=p.tp1, tp2=p.tp2, runaway=p.runaway_price, rr_tp1=p.rr_tp1, risk_usd=p.risk_usd, notional=p.notional,
        qty=p.qty, leverage=p.leverage, liq_price=p.liq_price, win_prob=p.win.p if p.win else None,
        flags=[{"code": f.code, "reason": f.reason} for f in p.flags],
        inputs={
            "score": an.score, "group_scores": an.group_scores, "atr": an.atr, "price": an.price,
            "factors": [[f.name, f.tf, round(f.score, 3)] for f in an.factors if f.scored],
            "costs": {"taker_bps": pc.taker_fee_bps, "maker_bps": pc.maker_fee_bps, "slip_bps": pc.slippage_bps},
            "max_hold_hours": settings.max_hold_hours, "spot_series": a.hl_spot_coin or (
                f"gt:{a.chain}:{a.pair_address}" if not a.hl_coin else None),
        },
        status="pending",
    )
    s.add(row)
    s.flush()
    return row
