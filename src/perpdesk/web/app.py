"""FastAPI application: server-rendered Jinja2 + HTMX pages, JSON for charts, cron/webhook endpoints."""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from perpdesk.config import get_settings
from perpdesk.db.session import _factory, migrate

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
log = logging.getLogger("perpdesk")
_migrated = False
_mlock = threading.Lock()


def ensure_db() -> None:
    global _migrated
    if _migrated:
        return
    with _mlock:
        if not _migrated:
            migrate()
            _migrated = True


def db() -> Iterator[Session]:
    ensure_db()
    s = _factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------- template helpers


def fmt_px(x: Any) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    x = float(x)
    if abs(x) >= 1000:
        return f"{x:,.1f}"
    if abs(x) >= 1:
        return f"{x:,.4g}" if abs(x) < 10 else f"{x:,.2f}" if abs(x) >= 100 else f"{x:,.3f}"
    return f"{x:.4g}"


def fmt_usd(x: Any, dec: int = 2) -> str:
    if x is None:
        return "–"
    v = float(x)
    return f"-${-v:,.{dec}f}" if v < 0 and round(v, dec) != 0 else f"${abs(v) if round(v, dec) == 0 else v:,.{dec}f}"


def fmt_pct(x: Any, dec: int = 2, signed: bool = True) -> str:
    if x is None:
        return "–"
    return f"{float(x):+.{dec}f}%" if signed else f"{float(x):.{dec}f}%"


def ago(iso: Any) -> str:
    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(str(iso)) if not isinstance(iso, datetime) else iso
    except ValueError:
        return str(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    sec = (datetime.now(UTC) - t).total_seconds()
    if sec < 60:
        return "just now"
    if sec < 3600:
        return f"{int(sec // 60)} min ago"
    if sec < 86400:
        return f"{int(sec // 3600)} h ago"
    return f"{int(sec // 86400)} d ago"


def ms_utc(ms: Any) -> str:
    if ms is None:
        return "–"
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


GRADE_ORDER = {"A": 0, "B": 1, "C": 2}
templates.env.filters.update(px=fmt_px, usd=fmt_usd, pct=fmt_pct, ago=ago, ms_utc=ms_utc)
templates.env.globals.update(app_name=lambda: get_settings().app_name)


# ---------------------------------------------------------------- auth

OPEN_PATHS = ("/login", "/static", "/healthz", "/api/cron", "/api/telegram")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        st = get_settings()
        if st.auth_enabled and not request.url.path.startswith(OPEN_PATHS) and not request.session.get("ok"):
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


def create_app() -> FastAPI:
    st = get_settings()
    logging.basicConfig(level=st.log_level)
    app = FastAPI(title=st.app_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=st.session_secret or "perpdesk-auth-disabled",
                       https_only=bool(st.public_domain), same_site="lax", max_age=14 * 86400)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    from perpdesk.web.routes import api, pages

    app.include_router(pages.router)
    app.include_router(api.router)
    return app
