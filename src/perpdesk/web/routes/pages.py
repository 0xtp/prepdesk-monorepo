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
from perpdesk.store import clear_overrides, load_overrides, set_override, settings_for
from perpdesk.tracking import report
from perpdesk.web.app import GRADE_ORDER, db, templates

router = APIRouter()


def _heartbeats(s: Session) -> dict[str, JobHeartbeat]:
    return {h.name: h for h in s.execute(select(JobHeartbeat)).scalars()}


def cards(s: Session) -> list[dict[str, Any]]:
    favs = list_favorites(s)
    views = {v.asset_id: v for v in s.execute(select(LatestView)).scalars()}
    out = []
    for f in favs:
        v = views.get(f.asset_id)
        out.append({"fav": f, "asset": f.asset, "group": f.group.name if f.group else "Favourites",
                    "v": v.payload if v else None})
    return out


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(db)) -> HTMLResponse:
    st = settings_for(s)
    cs = cards(s)
    grade_counts = {g: sum(1 for c in cs if c["v"] and c["v"].get("grade") == g) for g in "ABC"}
    sorted_cards = sorted(cs, key=lambda c: (GRADE_ORDER.get((c["v"] or {}).get("grade", "Z"), 9),
                                             -((c["v"] or {}).get("confidence") or 0)))
    return templates.TemplateResponse(request, "dashboard.html", {
        "cards": cs, "sorted_cards": sorted_cards, "groups": groups(s), "grade_counts": grade_counts,
        "hb": _heartbeats(s), "settings": st, "page": "dashboard",
    })


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
    return templates.TemplateResponse(request, "asset.html", {
        "a": a, "v": v.payload if v else None, "settings": settings_for(s), "rec": rec.as_dict(), "page": "asset",
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
    st = settings_for(s)
    health = s.execute(select(SourceHealthRow).order_by(SourceHealthRow.name)).scalars().all()
    return templates.TemplateResponse(request, "settings.html", {
        "settings": st, "masked": st.masked(), "overrides": load_overrides(s), "runtime_keys": RUNTIME_KEYS,
        "env": get_settings(), "health": health, "hb": _heartbeats(s), "saved": saved, "error": error,
        "page": "settings",
    })


@router.post("/settings")
async def settings_save(request: Request, s: Session = Depends(db)) -> RedirectResponse:
    form = await request.form()
    try:
        for k in RUNTIME_KEYS:
            if k not in form:
                continue
            raw = str(form[k]).strip()
            env_val = getattr(get_settings(), k)
            if raw == "" or raw == ("" if env_val is None else str(env_val)):
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
