"""End-to-end with a fresh SQLite DB and fake upstream APIs: tick, plan logging, scoring, pages, settings."""


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


def test_wallet_tracking_and_live_prices(app_env):
    c = app_env
    assert c.get("/api/cron/tick?budget=120", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    page = c.get("/wallet")
    assert page.status_code == 200 and "My trades" in page.text and "wallet-panel" not in page.text
    bad = c.post("/wallet/address", data={"address": "not-an-address"}, follow_redirects=False)
    assert bad.status_code == 303 and "error=" in bad.headers["location"]
    ok = c.post("/wallet/address", data={"address": fakes.WALLET.upper().replace("0X", "0x")}, follow_redirects=False)
    assert ok.status_code == 303 and "error" not in ok.headers["location"]
    assert "wallet-panel" in c.get("/wallet").text
    panel = c.post("/api/wallet/sync")
    assert panel.status_code == 200, panel.text
    t = panel.text
    assert "ETH" in t and "No stop-loss order" in t and "$1,000.00" in t
    assert "SOL" in t and "$98.85" in t  # 100 closed PnL - 0.95 exchange fees - 0.20 front-end fees
    assert "incl. $0.20 Phantom fee" in t and "What to change" in t and "Price ladder" in t
    assert "1 thing to fix" in t or "2 things to fix" in t
    assert "@107" not in t  # spot fills ignored
    # second sync is idempotent (no duplicate fills/trades)
    c.post("/api/wallet/sync")
    from perpdesk.db.models import WalletFill, WalletTrade
    from perpdesk.db.session import session_scope

    with session_scope() as s:
        assert s.query(WalletFill).count() == 4
        trades = {(x.coin, x.status): x for x in s.query(WalletTrade).all()}
        assert set(trades) == {("SOL", "closed"), ("ETH", "open")}
        eth = trades[("ETH", "open")]
        assert eth.tp_px == 2420 and eth.liq_px == 2600 and eth.leverage == 10 and eth.funding == 0.5
        sol = trades[("SOL", "closed")]
        assert sol.plan_id is None or sol.plan_direction in ("LONG", "SHORT")
    # tick syncs the wallet too
    out = c.get("/api/cron/tick?budget=5", headers={"Authorization": "Bearer s3cret"}).json()
    assert out["wallet"]["error"] is None
    # live prices
    from perpdesk.db.models import Asset

    with session_scope() as s:
        ids = [a.id for a in s.query(Asset).all()]
    r = c.get("/api/prices?ids=" + ",".join(map(str, ids)) + ",x")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == len(ids) and all(v["price"] > 0 and v["text"] for v in d.values())
    assert c.get("/api/prices").json() == {}
    home = c.get("/")
    assert "data-live-price" in home.text and "/api/prices" in home.text


def test_asset_page_leverage_slider(app_env):
    c = app_env
    assert c.get("/api/cron/tick?budget=120", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    from perpdesk.db.models import Asset
    from perpdesk.db.session import session_scope

    with session_scope() as s:
        a = s.query(Asset).filter(Asset.hl_coin == "SOL").one()
        aid, maxlev = a.id, a.max_leverage
    page = c.get(f"/asset/{aid}").text
    assert "levSlider(" in page and "Recommended" in page and "built-in method" not in page
    r = c.post(f"/asset/{aid}/plan", data={"margin": "500", "risk_usd": "", "risk_pct": "8"})
    assert r.status_code == 200 and "levSlider(" in r.text
    from perpdesk.pipeline import leverage_scenarios
    from perpdesk.store import settings_for

    with session_scope() as s:
        from perpdesk.db.models import LatestView

        a = s.get(Asset, aid)
        rows = leverage_scenarios(a, s.get(LatestView, aid).payload, settings_for(s), 500)
    assert rows[-1]["lev"] == maxlev and sum(r["recommended"] for r in rows) == 1
    rec = next(r for r in rows if r["recommended"])
    assert rec["mode"] == "plan" and rec["feasible"]
    assert all(r["mode"] != "plan" or r["risk_usd"] <= rec["risk_usd"] * 1.0001 + 0.01 for r in rows if r["feasible"])


def test_auto_page_mode_kill_resume(app_env):
    c = app_env
    p = c.get("/auto")
    assert p.status_code == 200 and "SIGNALS" in p.text and "Start paper trading" in p.text
    assert c.post("/auto/mode", data={"mode": "live"}, follow_redirects=False).headers["location"].startswith("/auto?msg=Only")
    assert c.get("/api/cron/auto").status_code == 401
    assert c.get("/api/cron/auto", headers={"Authorization": "Bearer s3cret"}).json()["mode"] == "signals"
    c.post("/auto/mode", data={"mode": "paper"})
    assert "saved=1" in c.post("/settings", data={"lab_coins": "SOL,BTC,ETH,HYPE,AVAX", "auto_coins": "SOL,ETH,HYPE"},
                               follow_redirects=False).headers["location"]
    p = c.get("/auto")
    assert "PAPER" in p.text and "Pause all" in p.text and "Leaderboard" in p.text and "Trend follower" in p.text
    out = c.get("/api/cron/tick?budget=120", headers={"Authorization": "Bearer s3cret"}).json()
    assert out["auto"]["mode"] == "paper" and not out["auto"]["errors"], out["auto"]
    a = c.get("/api/cron/auto", headers={"Authorization": "Bearer s3cret"}).json()
    assert a["auto"]["mode"] == "paper" and not a["auto"]["errors"], a
    k = c.post("/auto/kill", follow_redirects=False)
    assert k.status_code == 303 and "Kill%20switch" in k.headers["location"]
    assert "HALTED" in c.get("/auto").text
    c.post("/auto/resume")
    assert "HALTED" not in c.get("/auto").text
    s = c.get("/settings").text
    assert 'value="SOL,BTC,ETH,HYPE,AVAX"' in s and 'value="trend_follow,funding_carry"' in s
    bad = c.post("/settings", data={"lab_bots": "trend_follow,moonshot"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
