"""HTML pages."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.assets.favorites import groups, list_favorites
from perpdesk.config import RUNTIME_KEYS, get_settings
from perpdesk.db.models import Asset, BacktestRun, JobHeartbeat, LatestView, SetupStat, SourceHealthRow
from perpdesk.llm import usage as llm_usage
from perpdesk.pipeline import leverage_scenarios
from perpdesk.store import clear_overrides, load_overrides, set_override, settings_for
from perpdesk.tracking import report
from perpdesk.web.app import GRADE_ORDER, db, templates

router = APIRouter()


def _heartbeats(s: Session) -> dict[str, JobHeartbeat]:
    return {h.name: h for h in s.execute(select(JobHeartbeat)).scalars()}


def bot_signals(s: Session, st: Any) -> dict[str, Any]:
    """coin -> newest pending/open paper trade of an enabled proven bot."""
    from perpdesk.db.models import AutoTrade

    enabled = {"breakout", *st.lab_bots}
    out: dict[str, Any] = {}
    for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper", AutoTrade.status.in_(["pending", "open"]))
                       .order_by(AutoTrade.created_ms.desc())).scalars():
        if t.setup in enabled and t.coin not in out:
            out[t.coin] = t
    return out


def card_verdict(v: dict[str, Any] | None, a: Asset, bots: dict[str, Any], st: Any, now: int) -> Any:
    from perpdesk.auto.report import BOT_INFO
    from perpdesk.verdict import verdict

    if not v or v.get("error"):
        return None
    return verdict(v, bots.get(a.hl_coin or a.symbol), now, st, BOT_INFO)


def cards(s: Session, st: Any | None = None) -> list[dict[str, Any]]:
    from perpdesk.pipeline import now_ms

    st = st or settings_for(s)
    bots = bot_signals(s, st)
    now = now_ms()
    favs = list_favorites(s)
    views = {v.asset_id: v for v in s.execute(select(LatestView)).scalars()}
    out = []
    for f in favs:
        v = views.get(f.asset_id)
        pv = v.payload if v else None
        out.append({"fav": f, "asset": f.asset, "group": f.group.name if f.group else "Favourites",
                    "v": pv, "vd": card_verdict(pv, f.asset, bots, st, now)})
    return out


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    st = settings_for(s)
    cs = cards(s, st)
    take_count = sum(1 for c in cs if c["vd"] and c["vd"].take)
    grade_counts = {g: sum(1 for c in cs if c["v"] and c["v"].get("grade") == g) for g in "ABC"}
    sorted_cards = sorted(cs, key=lambda c: (GRADE_ORDER.get((c["v"] or {}).get("grade", "Z"), 9),
                                             -((c["v"] or {}).get("confidence") or 0)))
    return templates.TemplateResponse(request, "dashboard.html", {
        "cards": cs, "sorted_cards": sorted_cards, "take_count": take_count, "groups": groups(s), "grade_counts": grade_counts,
        "hb": _heartbeats(s), "settings": st, "page": "dashboard", **_dashboard_extras(s, st),
    })


def _dashboard_extras(s: Session, st: Any) -> dict[str, Any]:
    """Proven-strategy signals (the paper bots' open trades) and a warning when your own open positions are one
    correlated bet. Both are read from the database only (no exchange calls on page load)."""
    from perpdesk.auto.report import BOT_INFO
    from perpdesk.db.models import AutoTrade
    from perpdesk.pipeline import now_ms
    from perpdesk.wallet.report import positions_view

    enabled = {"breakout", *st.lab_bots}
    rows = [t for t in s.execute(select(AutoTrade).where(AutoTrade.mode == "paper",
                                                         AutoTrade.status.in_(["pending", "open"]))
                                 .order_by(AutoTrade.created_ms.desc())).scalars() if t.setup in enabled]
    corr: list[str] = []
    if st.watch_wallet:
        try:
            corr = positions_view(s, st)["correlation"]
        except Exception:
            corr = []
    from perpdesk.alerts import notify
    now = now_ms()
    day = notify.last_day(s, st, now)
    return {"proven": rows, "bot_labels": BOT_INFO, "now_ms": now, "wallet_corr": corr,
            "day": [{"t": t, "outcome": notify.outcome(t), "at": notify._t(t.created_ms, st)} for t in day],
            "next_check": notify._t(notify.next_check_ms(now), st),
            "next_check_min": round((notify.next_check_ms(now) - now) / 60_000)}


@router.get("/asset/{asset_id}", response_class=HTMLResponse)
def asset_page(asset_id: int, request: Request, s: Session = Depends(db)) -> HTMLResponse:
    a = s.get(Asset, asset_id)
    if a is None:
        return HTMLResponse("Not found", status_code=404)
    v = s.get(LatestView, asset_id)
    from perpdesk.tracking.service import live_rs
    from perpdesk.tracking.stats import r_stats

    coin = a.hl_coin or a.symbol
    rec = r_stats(live_rs(s, coin=coin))
    st = settings_for(s)
    lev_rows = leverage_scenarios(a, v.payload, st) if v else []
    from perpdesk.pipeline import now_ms

    vd = card_verdict(v.payload if v else None, a, bot_signals(s, st), st, now_ms())
    return templates.TemplateResponse(request, "asset.html", {
        "a": a, "v": v.payload if v else None, "settings": st, "rec": rec.as_dict(), "page": "asset",
        "lev_rows": lev_rows, "vd": vd,
        "timeframes": ["15m", "1h", "4h", "1d", "1w"],
    })


@router.get("/track", response_class=HTMLResponse)
def track(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    st = settings_for(s)
    return templates.TemplateResponse(request, "track.html", {"r": report.build(s, st), "settings": st,
                                                              "page": "track"})


@router.get("/wallet", response_class=HTMLResponse)
def wallet_page(request: Request, s: Session = Depends(db), error: str | None = None) -> HTMLResponse:
    st = settings_for(s)
    return templates.TemplateResponse(request, "wallet.html", {"settings": st, "page": "wallet", "error": error})


@router.post("/wallet/address")
def wallet_address(address: str = Form(""), s: Session = Depends(db)) -> RedirectResponse:
    try:
        set_override(s, "watch_wallet", address.strip() or None)
    except Exception as e:
        s.rollback()
        msg = str(e).split("Value error, ")[-1].split("[")[0].strip()[:200]
        return RedirectResponse(f"/wallet?error={msg}", status_code=303)
    return RedirectResponse("/wallet", status_code=303)


@router.post("/wallet/moonshot")
def wallet_moonshot(address: str = Form(""), budget: str = Form(""), s: Session = Depends(db)) -> RedirectResponse:
    from perpdesk.wallet import moonshot

    try:
        old = settings_for(s).moonshot_wallet
        set_override(s, "moonshot_wallet", address.strip() or None)
        set_override(s, "moonshot_budget_usd", budget.strip() or None)
        if old and old != address.strip().lower():
            from perpdesk.store import kv_set

            kv_set(s, moonshot.key(old), None)  # a new account starts a fresh record
    except Exception as e:
        s.rollback()
        msg = str(e).split("Value error, ")[-1].split("[")[0].strip()[:200]
        return RedirectResponse(f"/wallet?error={msg}#moonshot", status_code=303)
    return RedirectResponse("/wallet#moonshot", status_code=303)


@router.get("/auto", response_class=HTMLResponse)
def auto_page(request: Request, s: Session = Depends(db), msg: str | None = None) -> HTMLResponse:
    from perpdesk.auto import report as auto_report
    from perpdesk.pipeline import now_ms

    st = settings_for(s)
    return templates.TemplateResponse(request, "auto.html", {"r": auto_report.build(s, st, now_ms()), "settings": st,
                                                             "page": "auto", "msg": msg})


@router.post("/auto/mode")
def auto_mode(mode: str = Form(...), s: Session = Depends(db)) -> RedirectResponse:
    if mode not in ("signals", "paper"):
        return RedirectResponse("/auto?msg=Only signals and paper modes exist", status_code=303)
    set_override(s, "execution_mode", mode)
    return RedirectResponse(f"/auto?msg=Mode set to {mode}", status_code=303)


@router.post("/auto/kill")
async def auto_kill(s: Session = Depends(db)) -> RedirectResponse:
    from perpdesk.auto.engine import kill
    from perpdesk.pipeline import now_ms
    from perpdesk.sources.hyperliquid import Hyperliquid

    st = settings_for(s)
    out = await kill(s, Hyperliquid(st.hyperliquid_api_url, st.hl_rate_budget_per_min), now_ms())
    return RedirectResponse("/auto?msg=Kill switch: halted. " + ("; ".join(out) or "nothing was open"), status_code=303)


@router.post("/auto/resume")
def auto_resume(s: Session = Depends(db)) -> RedirectResponse:
    from perpdesk.auto import state

    state.resume(s)
    return RedirectResponse("/auto?msg=Resumed: new entries allowed again", status_code=303)


@router.get("/backtests", response_class=HTMLResponse)
def backtests(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    runs = s.execute(select(BacktestRun.id, BacktestRun.created_at, BacktestRun.coins, BacktestRun.summary)
                     .order_by(BacktestRun.id.desc()).limit(30)).all()
    latest = runs[0].id if runs else None
    stats: list[SetupStat] = []
    if latest:
        stats = list(s.execute(select(SetupStat).where(SetupStat.run_id == latest)
                          .order_by(SetupStat.setup, SetupStat.scope_kind, SetupStat.scope, SetupStat.direction)
                          ).scalars().all())
    return templates.TemplateResponse(request, "backtests.html", {"runs": runs, "stats": stats, "page": "backtests",
                                                                  "settings": settings_for(s)})


@router.get("/backtests/{run_id}/report", response_class=HTMLResponse)
def backtest_report(run_id: int, s: Session = Depends(db)) -> HTMLResponse:
    r = s.get(BacktestRun, run_id)
    if r is None:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(r.report_html)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(db), saved: str | None = None, error: str | None = None
                  ) -> HTMLResponse:
    from perpdesk.alerts import telegram as tg
    from perpdesk.store import kv_get

    st = settings_for(s)
    health = s.execute(select(SourceHealthRow).order_by(SourceHealthRow.name)).scalars().all()
    code = kv_get(s, tg.CODE_KEY)
    tgv = {"configured": tg.configured(st), "linked": tg.linked(s), "env_chat": bool(st.telegram_chat_id),
           "bot": kv_get(s, "telegram:bot"), "code": code["code"] if isinstance(code, dict) else None}
    return templates.TemplateResponse(request, "settings.html", {
        "settings": st, "masked": st.masked(), "overrides": load_overrides(s), "runtime_keys": RUNTIME_KEYS,
        "env": get_settings(), "health": health, "hb": _heartbeats(s), "saved": saved, "error": error,
        "page": "settings", "tg": tgv, "tg_msg": request.query_params.get("tg"),
    })


def _public_base(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    local = host.startswith(("localhost", "127.0.0.1", "testserver"))
    return f"{'http' if local else 'https'}://{host}"


@router.post("/telegram/connect")
async def telegram_connect(request: Request, s: Session = Depends(db)) -> RedirectResponse:
    """Point the bot's webhook at this deployment and create a one-time code to link your chat."""
    from perpdesk.alerts import telegram as tg
    from perpdesk.store import kv_set

    st = settings_for(s)
    if not tg.configured(st):
        return RedirectResponse("/settings?tg=Set TELEGRAM_BOT_TOKEN in Vercel first, then redeploy.", status_code=303)
    try:
        me = await tg.api(st, "getMe")
        user = (me or {}).get("result", {}).get("username", "")
        url = f"{_public_base(request)}/api/telegram/{tg.webhook_secret(st)}"
        await tg.api(st, "setWebhook", {"url": url, "allowed_updates": ["message"], "drop_pending_updates": True})
    except Exception as e:
        msg = str(e).split(": ", 1)[-1][:160]
        return RedirectResponse(f"/settings?tg=Telegram refused: {msg}", status_code=303)
    kv_set(s, "telegram:bot", user)
    tg.new_link_code(s)
    s.commit()
    return RedirectResponse("/settings?tg=Open the link below and press Start in Telegram.#telegram", status_code=303)


@router.post("/telegram/test")
async def telegram_test(s: Session = Depends(db)) -> RedirectResponse:
    from perpdesk.alerts import notify
    from perpdesk.alerts import telegram as tg
    from perpdesk.pipeline import now_ms

    st = settings_for(s)
    ok = await tg.notify(s, st, "🔔 <b>PerpDesk test alert</b>\nTAKE signals, fills, closes and the daily scorecard "
                         "will arrive here.\n\n" + notify.summary_text(s, st, now_ms()), html=True)
    return RedirectResponse(f"/settings?tg={'Test message sent.' if ok else 'Not sent: link your chat first.'}#telegram",
                            status_code=303)


@router.post("/telegram/unlink")
def telegram_unlink(s: Session = Depends(db)) -> RedirectResponse:
    from perpdesk.alerts import telegram as tg

    tg.unlink(s)
    s.commit()
    return RedirectResponse("/settings?tg=Chat unlinked. No more alerts.#telegram", status_code=303)


@router.post("/settings")
async def settings_save(request: Request, s: Session = Depends(db)) -> RedirectResponse:
    form = await request.form()
    try:
        for k in RUNTIME_KEYS:
            if k not in form:
                continue
            raw = str(form[k]).strip()
            env_val = getattr(get_settings(), k)
            env_str = ",".join(map(str, env_val)) if isinstance(env_val, list) else ("" if env_val is None else str(env_val))
            if raw == "" or raw == env_str:
                set_override(s, k, None)
                continue
            set_override(s, k, raw)
    except Exception as e:
        s.rollback()
        return RedirectResponse(f"/settings?error={type(e).__name__}: {str(e)[:200]}", status_code=303)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/reset")
def settings_reset(s: Session = Depends(db)) -> RedirectResponse:
    clear_overrides(s)
    return RedirectResponse("/settings?saved=reset", status_code=303)


@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    st = settings_for(s)
    health = s.execute(select(SourceHealthRow).order_by(SourceHealthRow.name)).scalars().all()
    return templates.TemplateResponse(request, "usage.html", {"u": llm_usage.summary(s, st), "health": health,
                                                              "settings": st, "page": "usage"})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": error, "page": "login"})


@router.post("/login")
def login(request: Request, password: str = Form(...), s: Session = Depends(db)) -> RedirectResponse:
    import bcrypt

    from perpdesk.store import kv_get, kv_set

    st = get_settings()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    import time

    key = f"login:{ip}:{int(time.time() // 60)}"
    tries = int(kv_get(s, key) or 0)
    if tries >= 5:
        return RedirectResponse("/login?error=Too many attempts, wait a minute", status_code=303)
    kv_set(s, key, tries + 1)
    s.commit()
    ok = bool(st.ui_password_hash) and bcrypt.checkpw(password.encode(), st.ui_password_hash.encode())
    if not ok:
        return RedirectResponse("/login?error=Wrong password", status_code=303)
    request.session["ok"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
