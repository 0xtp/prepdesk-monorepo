"""HTMX partials, JSON for charts, favourites, trades, cron + Telegram webhooks, health."""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.analysis.indicators import ema
from perpdesk.assets import favorites as fav
from perpdesk.assets.registry import search as registry_search
from perpdesk.db.models import Asset, JobHeartbeat, LatestView, PlanRow, UserTrade
from perpdesk.pipeline import (
    Context,
    analyse_asset,
    dex_frames,
    leverage_scenarios,
    load_context,
    now_ms,
    perp_frames,
)
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import load_candles, persist_health, settings_for
from perpdesk.timeutil import index_ms
from perpdesk.web.app import db, templates

router = APIRouter()
_ctx_cache: dict[str, tuple[float, Context]] = {}


def _hl(s: Session) -> Hyperliquid:
    st = settings_for(s)
    return Hyperliquid(st.hyperliquid_api_url, st.hl_rate_budget_per_min)


async def _context(s: Session) -> Context:
    hit = _ctx_cache.get("ctx")
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    ctx = await load_context(s, _hl(s), settings_for(s))
    _ctx_cache["ctx"] = (time.monotonic(), ctx)
    return ctx


def _card(request: Request, s: Session, a: Asset) -> HTMLResponse:
    v = s.get(LatestView, a.id)
    f = next((x for x in fav.list_favorites(s) if x.asset_id == a.id), None)
    from perpdesk.web.routes.pages import bot_signals, card_verdict

    st = settings_for(s)
    pv = v.payload if v else None
    c = {"fav": f, "asset": a, "group": f.group.name if f and f.group else "Favourites", "v": pv,
         "vd": card_verdict(pv, a, bot_signals(s, st), st, now_ms())}
    return templates.TemplateResponse(request, "_card.html", {"c": c, "settings": st})


@router.post("/api/assets/{asset_id}/refresh", response_class=HTMLResponse)
async def refresh(asset_id: int, request: Request, s: Session = Depends(db)) -> HTMLResponse:
    a = s.get(Asset, asset_id)
    if a is None:
        return HTMLResponse("", status_code=404)
    await analyse_asset(s, _hl(s), a, await _context(s), settings_for(s), log=True)
    persist_health(s)
    s.commit()
    return _card(request, s, a)


@router.get("/api/assets/{asset_id}/card", response_class=HTMLResponse)
def card(asset_id: int, request: Request, s: Session = Depends(db)) -> HTMLResponse:
    a = s.get(Asset, asset_id)
    if a is None:
        return HTMLResponse("", status_code=404)
    return _card(request, s, a)


@router.get("/api/assets/{asset_id}/candles")
async def candles(asset_id: int, tf: str = "1h", s: Session = Depends(db)) -> JSONResponse:
    a = s.get(Asset, asset_id)
    if a is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if tf not in ("15m", "1h", "4h", "1d", "1w"):
        tf = "1h"
    df: pd.DataFrame | None
    if a.hl_coin and a.perp_available:
        df = (await perp_frames(s, _hl(s), a.hl_coin, [tf]))[tf]
    elif a.hl_spot_coin:
        df = (await perp_frames(s, _hl(s), a.hl_spot_coin, [tf], series_prefix="hlspot"))[tf]
    else:
        df = (await dex_frames(s, a, [tf, "1d"] if tf == "1w" else [tf], {})).get(tf)
        if df is None:
            df = load_candles(s, f"gt:{a.chain}:{a.pair_address}", tf)
    s.commit()
    if df is None or df.empty:
        return JSONResponse({"error": f"no {tf} candles available", "candles": []})
    t = (index_ms(df) // 1000).tolist()
    out: dict[str, Any] = {
        "candles": [{"time": int(ti), "open": o, "high": h, "low": lo, "close": c}
                    for ti, o, h, lo, c in zip(t, df["open"], df["high"], df["low"], df["close"], strict=True)],
        "volume": [{"time": int(ti), "value": float(v)} for ti, v in zip(t, df["volume"], strict=True)],
    }
    for n in (20, 50, 200):
        e = ema(df["close"], n)
        out[f"ema{n}"] = [{"time": int(ti), "value": float(v)} for ti, v in zip(t, e, strict=True) if v == v]
    v = s.get(LatestView, a.id)
    if v and not v.payload.get("error"):
        p = v.payload["plan"]
        out["plan"] = {k: p[k] for k in ("entry", "sl", "tp1", "tp2", "liq", "zone", "direction")}
        out["levels"] = v.payload.get("levels", [])[:40]
        out["cme_gaps"] = v.payload.get("cme_gaps", [])
        out["range"] = v.payload.get("range")
    return JSONResponse(out)


@router.post("/asset/{asset_id}/plan", response_class=HTMLResponse)
async def recompute_plan(asset_id: int, request: Request, margin: float = Form(...), risk_usd: str = Form(""),
                         risk_pct: str = Form(""), s: Session = Depends(db)) -> HTMLResponse:
    a = s.get(Asset, asset_id)
    if a is None:
        return HTMLResponse("", status_code=404)
    st = settings_for(s)
    r_usd = float(risk_usd) if risk_usd.strip() else None
    r_pct = float(risk_pct) if risk_pct.strip() else None
    res = await analyse_asset(s, _hl(s), a, await _context(s), st, margin=margin, risk_usd=r_usd, risk_pct=r_pct,
                              save=False)
    lev_rows = leverage_scenarios(a, res.view, st, margin) if res.view else []
    from perpdesk.web.routes.pages import bot_signals, card_verdict

    vd = card_verdict(res.view, a, bot_signals(s, st), st, now_ms())
    return templates.TemplateResponse(request, "_plan.html", {"v": res.view, "a": a, "settings": st,
                                                              "lev_rows": lev_rows, "vd": vd,
                                                              "inputs": {"margin": margin, "risk_usd": risk_usd,
                                                                         "risk_pct": risk_pct}})


@router.get("/asset/{asset_id}/explain", response_class=HTMLResponse)
async def explain(asset_id: int, request: Request, s: Session = Depends(db)) -> HTMLResponse:
    from perpdesk.llm.explain import explain as run_explain

    v = s.get(LatestView, asset_id)
    if v is None or v.payload.get("error"):
        return HTMLResponse('<p class="text-zinc-500">No plan to explain yet.</p>')
    out = await run_explain(s, settings_for(s), v.payload)
    return templates.TemplateResponse(request, "_explain.html", {"e": out})


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", s: Session = Depends(db)) -> HTMLResponse:
    results, error = [], None
    if len(q.strip()) >= 2:
        try:
            results = await registry_search(q, _hl(s), settings_for(s).coingecko_api_key)
        except Exception as e:
            error = f"search unavailable: {e}"
    have = {a.key for a in s.execute(select(Asset)).scalars()}
    favs = {f.asset.key for f in fav.list_favorites(s)}
    return templates.TemplateResponse(request, "_search.html", {"results": results, "error": error, "q": q,
                                                                "favs": favs, "have": have,
                                                                "groups": fav.groups(s)})


@router.post("/favorites", response_class=HTMLResponse)
async def add_favorite(key: str = Form(...), group: str = Form(""), s: Session = Depends(db)) -> Response:
    try:
        a = await fav.add_favorite(s, key, _hl(s), group or None)
        s.commit()
    except Exception as e:
        return HTMLResponse(f'<span class="text-rose-400">Could not add: {e}</span>')
    return HTMLResponse(f'<span class="text-emerald-400">Added {a.symbol} ✓</span>',
                        headers={"HX-Trigger": "favorites-changed"})


@router.post("/favorites/seed")
async def seed_favorites(s: Session = Depends(db)) -> Response:
    missing = await fav.seed_defaults(s, settings_for(s), _hl(s))
    s.commit()
    if missing and any("postponed" in m for m in missing):
        return HTMLResponse(f'<span class="text-rose-400">{missing[0]}</span>')
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/favorites/{asset_id}/delete")
def remove_favorite(asset_id: int, s: Session = Depends(db)) -> Response:
    fav.remove_favorite(s, asset_id)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/favorites/{asset_id}/group")
def set_group(asset_id: int, group: str = Form(...), s: Session = Depends(db)) -> Response:
    if group.strip():
        fav.move_to_group(s, asset_id, group.strip()[:60])
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/favorites/reorder")
async def reorder(request: Request, s: Session = Depends(db)) -> JSONResponse:
    body = await request.json()
    for grp in body.get("groups", []):
        fav.reorder(s, [int(x) for x in grp.get("ids", [])], grp.get("name") or None)
    return JSONResponse({"ok": True})


@router.post("/trades", response_class=HTMLResponse)
def took(asset_id: int = Form(...), direction: str = Form(...), fill_price: float = Form(...), qty: float = Form(...),
         leverage: int = Form(1), sl: str = Form(""), s: Session = Depends(db)) -> HTMLResponse:
    a = s.get(Asset, asset_id)
    if a is None:
        return HTMLResponse("unknown asset", status_code=404)
    coin = a.hl_coin or a.symbol
    plan = s.execute(select(PlanRow).where(PlanRow.coin == coin).order_by(PlanRow.created_ms.desc())).scalars().first()
    t = UserTrade(plan_id=plan.id if plan and plan.direction == direction else None, coin=coin, direction=direction,
                  fill_price=fill_price, qty=qty, leverage=leverage, sl=float(sl) if sl.strip() else None,
                  opened_at=datetime.now(UTC), notes="")
    s.add(t)
    return HTMLResponse('<span class="text-emerald-400">Recorded ✓ — close it on the Track Record page</span>')


@router.post("/trades/{trade_id}/close")
def close_trade(trade_id: int, exit_price: float = Form(...), s: Session = Depends(db)) -> Response:
    t = s.get(UserTrade, trade_id)
    if t is not None:
        t.exit_price, t.closed_at = exit_price, datetime.now(UTC)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int, s: Session = Depends(db)) -> Response:
    t = s.get(UserTrade, trade_id)
    if t is not None:
        s.delete(t)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


# ---------------------------------------------------------------- live prices + watch-only wallet


@router.get("/api/prices")
async def prices(ids: str = "", s: Session = Depends(db)) -> JSONResponse:
    from perpdesk.prices import live_prices
    from perpdesk.web.app import fmt_pct, fmt_px

    want = [int(x) for x in ids.split(",") if x.strip().isdigit()][:100]
    assets = list(s.execute(select(Asset).where(Asset.id.in_(want))).scalars()) if want else []
    out = await live_prices(_hl(s), assets)
    return JSONResponse({str(k): {**v, "text": fmt_px(v["price"]), "change_text": fmt_pct(v["change24"])
                                  if v.get("change24") is not None else ""} for k, v in out.items()},
                        headers={"Cache-Control": "no-store"})


async def _wallet_panel(request: Request, s: Session, force: bool) -> HTMLResponse:
    from perpdesk.wallet import report as wreport
    from perpdesk.wallet.sync import sync_wallet

    st = settings_for(s)
    res = await sync_wallet(s, _hl(s), st, force=force)
    s.commit()
    return templates.TemplateResponse(request, "_wallet_panel.html", {
        "w": wreport.positions_view(s, st), "h": wreport.history_view(s, st), "sync": res, "settings": st})


@router.get("/api/wallet/panel", response_class=HTMLResponse)
async def wallet_panel(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    return await _wallet_panel(request, s, force=False)


@router.post("/api/wallet/sync", response_class=HTMLResponse)
async def wallet_sync(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    return await _wallet_panel(request, s, force=True)


@router.get("/api/moonshot/panel", response_class=HTMLResponse)
async def moonshot_panel(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    from perpdesk.wallet import moonshot

    st = settings_for(s)
    err = None
    try:
        res = await moonshot.sync_cached(s, _hl(s), st, now_ms())
        if res and res.get("error"):
            err = res["error"]
        s.commit()
    except Exception as e:  # show the last known state if Hyperliquid is unreachable
        s.rollback()
        err = f"Could not read the moonshot account: {type(e).__name__}"
    return templates.TemplateResponse(request, "_moonshot.html", {
        "m": moonshot.view(s, st, moonshot.main_value(s, st)), "error": err, "settings": st})


# ---------------------------------------------------------------- cron / webhooks / health


def _cron_ok(request: Request, s: Session) -> bool:
    secret = settings_for(s).cron_secret
    if not secret:
        return False
    auth = request.headers.get("authorization", "")
    given = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("key", "")
    return hmac.compare_digest(given, secret)


@router.api_route("/api/cron/tick", methods=["GET", "POST"])
async def cron_tick(request: Request, s: Session = Depends(db)) -> JSONResponse:
    if not _cron_ok(request, s):
        return JSONResponse({"error": "unauthorized (set CRON_SECRET and send Authorization: Bearer <secret>)"},
                            status_code=401)
    from perpdesk.jobs.tick import tick

    out = await tick(s, budget_s=float(request.query_params.get("budget", 45)))
    return JSONResponse(out)


@router.api_route("/api/cron/auto", methods=["GET", "POST"])
async def cron_auto(request: Request, s: Session = Depends(db)) -> JSONResponse:
    """Auto-trader only (paper). Schedule every 2-5 minutes so 4h signals are acted on promptly."""
    if not _cron_ok(request, s):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from perpdesk.jobs.tick import auto_tick

    return JSONResponse(await auto_tick(s, budget_s=float(request.query_params.get("budget", 40))))


@router.api_route("/api/cron/daily", methods=["GET", "POST"])
async def cron_daily(request: Request, s: Session = Depends(db)) -> JSONResponse:
    if not _cron_ok(request, s):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from perpdesk.jobs.tick import daily

    return JSONResponse(await daily(s), status_code=200)


@router.post("/api/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request, s: Session = Depends(db)) -> JSONResponse:
    """Telegram updates. Read-only: links your chat, answers /status, /summary, /check SOL. No trading commands."""
    from perpdesk.alerts import notify
    from perpdesk.alerts import telegram as tg

    st = settings_for(s)
    want = tg.webhook_secret(st)
    if not (tg.configured(st) and want and hmac.compare_digest(secret, want)):
        return JSONResponse({"ok": False}, status_code=403)
    upd = await request.json()
    msg = upd.get("message") or {}
    uid = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    chat = (msg.get("chat") or {}).get("id")
    if not chat:
        return JSONResponse({"ok": True})
    if not tg.allowed_user(s, st, uid):
        if tg.try_link(s, text, chat, uid):
            s.commit()
            await tg.send(st, chat, "✅ Linked. PerpDesk alerts will come to this chat only.\n"
                                    "Commands: /status · /summary · /check SOL")
        return JSONResponse({"ok": True})  # strangers get no reply at all
    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd == "/check":
        parts = text.split()
        sym = parts[1].upper() if len(parts) > 1 else ""
        a = s.execute(select(Asset).where((Asset.hl_coin == sym) | (Asset.symbol == sym))).scalars().first()
        if a is None:
            await tg.send(st, chat, f"{sym or '?'} is not tracked. Add it as a favourite in the app first.")
        else:
            res = await analyse_asset(s, _hl(s), a, await _context(s), st, log=True)
            s.commit()
            await tg.send(st, chat, tg.format_check(res.view))
    elif cmd == "/status":
        await tg.send(st, chat, _tg_status(s, st))
    elif cmd == "/summary":
        await tg.send(st, chat, notify.summary_text(s, st, now_ms()), html=True)
    else:
        await tg.send(st, chat, "Read-only bot. Commands: /status (open paper trades and next check) · /summary "
                                "(last 24h) · /check SOL (general analysis).")
    return JSONResponse({"ok": True})


def _tg_status(s: Session, st: Any) -> str:
    from perpdesk.alerts import notify
    from perpdesk.db.models import AutoTrade

    now = now_ms()
    rows = [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper",
                                                         AutoTrade.status.in_(["pending", "open"]))
                                 .order_by(AutoTrade.created_ms.desc())).scalars()
            if t.setup in notify.enabled_bots(st)]
    lines = [f"Paper trading: {st.execution_mode}. Next 4h check: {notify._t(notify.next_check_ms(now), st)}."]
    for t in rows[:12]:
        lines.append(f"• {t.coin} {t.direction} {notify._label(t.setup)} — {notify.outcome(t)}"
                     f"{' · TAKE' if t.take else ''}")
    if not rows:
        lines.append("No open paper trades.")
    hb = s.get(JobHeartbeat, "auto")
    if hb is not None:
        lines.append(f"Auto scheduler last ran {hb.last_run:%H:%M} UTC ({'ok' if hb.ok else 'error'}).")
    return "\n".join(lines)


@router.get("/healthz")
def healthz(s: Session = Depends(db)) -> JSONResponse:
    hb = s.get(JobHeartbeat, "tick")
    age = None
    if hb is not None:
        lr = hb.last_run if hb.last_run.tzinfo else hb.last_run.replace(tzinfo=UTC)
        age = int((datetime.now(UTC) - lr).total_seconds())
    return JSONResponse({"ok": True, "db": "ok", "last_tick_age_s": age, "last_tick_ok": hb.ok if hb else None})
