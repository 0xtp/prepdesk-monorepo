"""End-to-end with a fresh SQLite DB and fake upstream APIs: tick, plan logging, scoring, pages, settings."""

import asyncio
import os

import pytest
import respx

import fakes


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setenv("DEFAULT_FAVORITES", "Majors:BTC,ETH|Large:SOL,HYPE")
    from perpdesk.config import get_settings
    from perpdesk.db import session as dbs
    from perpdesk.sources import base
    from perpdesk.web import app as webapp

    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
    base.CACHE.clear()
    base._client = None
    webapp._migrated = False
    from perpdesk.web.routes import api

    api._ctx_cache.clear()
    with respx.mock(assert_all_called=False) as router:
        fakes.install(router)
        from fastapi.testclient import TestClient

        yield TestClient(webapp.create_app())
    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()


def test_tick_seeds_analyses_and_logs_plans(app_env):
    c = app_env
    assert c.get("/api/cron/tick").status_code == 401
    r = c.get("/api/cron/tick?budget=120", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["seed_missing"] == [] and set(out["analysed"]) == {"BTC", "ETH", "SOL", "HYPE"}, out
    assert out["errors"] == []
    page = c.get("/")
    assert page.status_code == 200
    for sym in ("BTC", "ETH", "SOL", "HYPE"):
        assert sym in page.text
    assert "Signals are probabilistic. Not financial advice." in page.text
    from perpdesk.db.models import PlanRow
    from perpdesk.db.session import session_scope

    with session_scope() as s:
        plans = s.query(PlanRow).all()
        assert len(plans) == 4
        assert all(p.direction in ("LONG", "SHORT") and p.grade in ("A", "B", "C") for p in plans)
    # a second tick does not duplicate active plans and scores them
    r2 = c.get("/api/cron/tick?budget=120", headers={"Authorization": "Bearer s3cret"})
    assert r2.json()["scoring"]["checked"] == 4
    with session_scope() as s:
        assert s.query(PlanRow).count() <= 8


def test_asset_page_chart_plan_and_explain(app_env):
    c = app_env
    c.get("/api/cron/tick?budget=120", headers={"Authorization": "Bearer s3cret"})
    from perpdesk.db.models import Asset
    from perpdesk.db.session import session_scope

    with session_scope() as s:
        sol = s.query(Asset).filter_by(symbol="SOL").one().id
    page = c.get(f"/asset/{sol}")
    assert page.status_code == 200 and "Factors" in page.text and "Copy plan" in page.text
    data = c.get(f"/api/assets/{sol}/candles?tf=1h").json()
    assert data["candles"] and data["ema20"] and data["plan"]["sl"]
    plan = c.post(f"/asset/{sol}/plan", data={"margin": "300", "risk_usd": "15", "risk_pct": ""})
    assert plan.status_code == 200 and "Stop loss" in plan.text
    ex = c.get(f"/asset/{sol}/explain")
    assert ex.status_code == 200 and "built-in" in ex.text
    card = c.post(f"/api/assets/{sol}/refresh")
    assert card.status_code == 200 and "SOL" in card.text
    took = c.post("/trades", data={"asset_id": sol, "direction": "LONG", "fill_price": "106", "qty": "1", "leverage": "2",
                                   "sl": "104"})
    assert "Recorded" in took.text
    tr = c.get("/track")
    assert tr.status_code == 200 and "My trades" in tr.text


def test_search_add_spot_only_token(app_env):
    c = app_env
    r = c.get("/search?q=0xabc0000000000000000000000000000000000001")
    assert r.status_code == 200 and "Spot only" in r.text
    r = c.post("/favorites", data={"key": "dex:base:0xabc0000000000000000000000000000000000001", "group": ""})
    assert "Added" in r.text
    from perpdesk.db.models import Asset
    from perpdesk.db.session import session_scope

    with session_scope() as s:
        a = s.query(Asset).filter_by(symbol="TST").one()
        assert not a.perp_available
        aid = a.id
    card = c.post(f"/api/assets/{aid}/refresh")
    assert "Spot only" in card.text
    page = c.get(f"/asset/{aid}")
    assert "Spot view only" in page.text
    s2 = c.get("/search?q=SOL")
    assert "Perp ✓" in s2.text


def test_settings_overrides_and_pages(app_env):
    c = app_env
    assert c.post("/settings", data={"weights_trend": "0.4", "min_confidence": "55"}, follow_redirects=False).status_code == 303
    page = c.get("/settings")
    assert "override (env" in page.text
    bad = c.post("/settings", data={"risk_pct_of_margin": "50"}, follow_redirects=False)
    assert "error" in bad.headers["location"]
    assert c.post("/settings/reset", follow_redirects=False).status_code == 303
    for path in ("/backtests", "/usage", "/healthz", "/login"):
        assert c.get(path).status_code == 200


def test_auth(monkeypatch, tmp_path):
    import bcrypt

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/a.db")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("UI_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    from perpdesk.config import get_settings
    from perpdesk.db import session as dbs
    from perpdesk.web import app as webapp

    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
    webapp._migrated = False
    from fastapi.testclient import TestClient

    c = TestClient(webapp.create_app())
    assert c.get("/", follow_redirects=False).status_code == 303
    assert "Wrong" in c.post("/login", data={"password": "nope"}).text
    c.post("/login", data={"password": "pw"})
    assert c.get("/").status_code == 200
    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
