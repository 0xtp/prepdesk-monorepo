"""Telegram alerts: local time, TAKE tagging at entry, TAKE/close/halt alerts (sent once), quiet hours, daily
summary, chat linking through the webhook, and the token never leaking into logs."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

import fakes
from perpdesk.alerts import notify
from perpdesk.alerts import telegram as tg
from perpdesk.alerts.clock import hhmm, in_quiet_hours, parse_tz
from perpdesk.config import Settings
from perpdesk.verdict import BREAKOUT_SHORT_WEAK, entry_tag

NOW = int(datetime(2026, 10, 20, 8, 5, tzinfo=UTC).timestamp() * 1000)
MIN = 60_000
TOKEN = "123456:ABC-test_token"


def test_clock():
    assert parse_tz("+05:30") == timezone(timedelta(hours=5, minutes=30))
    assert parse_tz("UTC") is UTC and parse_tz("Mars/Base") is None
    assert hhmm(NOW, "+05:30") == "13:35 +05:30"
    assert in_quiet_hours(NOW, "UTC", "07-09") and not in_quiet_hours(NOW, "UTC", "09-07")
    assert in_quiet_hours(NOW, "+05:30", "23-14")  # wraps midnight
    with pytest.raises(ValueError):
        Settings(alert_tz="Nowhere/Land")
    with pytest.raises(ValueError):
        Settings(alert_quiet_hours="1am-7am")


def _trade(**kw):
    t = dict(setup="breakout", direction="LONG", exit_model="fixed", entry=100.0, sl=97.0, tp1=105.0, tp2=110.0)
    t.update(kw)
    return SimpleNamespace(**t)


def _view(age_min=5, flags=()):
    return {"updated_at": datetime.fromtimestamp((NOW - age_min * MIN) / 1000, UTC).isoformat(), "direction": "LONG",
            "flags": [{"code": c, "reason": r} for c, r in flags], "levels": []}


def test_entry_tag_rules():
    st = Settings()
    assert entry_tag(_view(), _trade(), NOW, st) == (True, "fresh signal, no warnings")
    assert entry_tag(_view(), _trade(direction="SHORT"), NOW, st) == (False, BREAKOUT_SHORT_WEAK)
    assert entry_tag(_view(), _trade(direction="SHORT"), NOW, Settings(breakout_short_take=True))[0]
    assert entry_tag(_view(), _trade(setup="trend_follow", direction="SHORT"), NOW, st)[0]
    ev = entry_tag(_view(flags=[("event", "CPI at 12:30")]), _trade(), NOW, st)
    assert ev == (False, "⚠ CPI at 12:30")
    assert entry_tag(_view(flags=[("low_rr", "R:R 0.9")]), _trade(), NOW, st)[0]  # composite-only flag
    assert "not checked" in entry_tag(_view(age_min=200), _trade(), NOW, st)[1]
    assert entry_tag(None, _trade(setup="funding_carry", direction="CARRY", exit_model="carry"), NOW, st)[0]


# ---------------------------------------------------------------- notify with a real (sqlite) database

@pytest.fixture()
def s(tmp_path):
    from perpdesk.db import session as dbs
    from perpdesk.db.models import Asset

    url = f"sqlite:///{tmp_path}/n.db"
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
    dbs.migrate(url)
    sess = dbs._factory(url)()
    sess.add(Asset(key="SOL", symbol="SOL", hl_coin="SOL", perp_available=True, sz_decimals=2, max_leverage=20,
                   asset_class="Large", margin_tiers=[]))
    sess.commit()
    yield sess
    sess.close()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()


@pytest.fixture()
def sent(monkeypatch):
    out = []

    async def fake_send(settings, chat, text):
        out.append((chat, text))

    monkeypatch.setattr(tg, "send", fake_send)
    return out


def _add(s, **kw):
    from perpdesk.db.models import AutoTrade
    from perpdesk.store import utcnow

    base = dict(mode="paper", coin="SOL", asset_id=1, setup="breakout", direction="LONG", status="pending",
                signal_ms=NOW - 5 * MIN, created_ms=NOW - 4 * MIN, entry_type="market", entry=100.0, sl=97.0,
                tp1=105.0, tp2=110.0, runaway=130.0, expires_ms=NOW + 3_600_000, max_hold_ms=3_600_000, qty=1.0,
                leverage=2, notional=100.0, margin=50.0, risk_usd=3.0, entry_fee=0, exit_fee=0, slippage=0,
                client_id=f"t:{kw.get('direction', 'LONG')}:{kw.get('created_ms', 0)}", tp1_hit=False,
                updated_at=utcnow())
    base.update(kw)
    t = AutoTrade(**base)
    s.add(t)
    s.commit()
    return t


def _settings(**kw):
    return Settings(**{"telegram_bot_token": TOKEN, "execution_mode": "paper", "lab_bots": ["trend_follow"],
                       "alert_daily_hour": None, **kw})


def _link(s, chat=42, uid=7):
    from perpdesk.store import kv_set

    kv_set(s, tg.LINK_KEY, {"chat_id": chat, "user_id": uid})
    s.commit()


def test_take_alert_sent_once_then_close(s, sent):
    st = _settings(alert_account_usd=2000, alert_risk_pct=1, alert_tz="+05:30")
    _link(s)
    long_ = _add(s)
    short = _add(s, direction="SHORT", created_ms=NOW - 3 * MIN)  # breakout short: tagged skip, no alert
    out = asyncio.run(notify.after_auto(s, None, None, st, NOW, None))
    assert out["take_alerts"] == 1 and not out.get("error"), out
    assert long_.take is True and short.take is False and short.take_reason == BREAKOUT_SHORT_WEAK
    chat, text = sent[0]
    assert chat == 42 and "✅ TAKE · SOL LONG · Breakout" in text and "Stop: $97.000" in text
    assert "risk 1% of $2,000 = $20.00 → 6.667 SOL" in text and "Valid until 14:01 +05:30" in text
    assert "app.hyperliquid.xyz/trade/SOL" in text and TOKEN not in text
    asyncio.run(notify.after_auto(s, None, None, st, NOW + MIN, None))
    assert len(sent) == 1  # never repeated
    long_.status, long_.exit_reason, long_.r_multiple, long_.pnl_usd, long_.exit_px = "closed", "tp2", 2.1, 6.3, 110.0
    short.status, short.exit_reason, short.r_multiple = "closed", "sl", -1.0
    s.commit()
    asyncio.run(notify.after_auto(s, None, None, st, NOW + 60 * MIN, None))
    assert len(sent) == 2 and "SOL LONG · Breakout hit TP2" in sent[1][1] and "+2.10R" in sent[1][1]


def test_no_chat_no_send_and_quiet_hours(s, sent):
    _add(s)
    asyncio.run(notify.after_auto(s, None, None, _settings(), NOW, None))
    assert sent == []  # not linked
    _link(s)
    t = _add(s, created_ms=NOW - 2 * MIN, client_id="q")
    asyncio.run(notify.after_auto(s, None, None, _settings(alert_quiet_hours="07-09"), NOW, None))
    assert sent == [] and t.alert_open_ms is not None and t.alert_open_ms < 0  # suppressed for good
    t.status, t.exit_reason, t.r_multiple = "closed", "sl", -1.0
    s.commit()
    asyncio.run(notify.after_auto(s, None, None, _settings(), NOW + 30 * MIN, None))
    assert sent == []  # no close alert for a TAKE you were never told about


def test_old_untagged_trade_is_not_alerted(s, sent):
    _link(s)
    t = _add(s, created_ms=NOW - 5 * 3_600_000, client_id="old")
    asyncio.run(notify.after_auto(s, None, None, _settings(), NOW, None))
    assert t.take is False and "not checked" in t.take_reason and sent == []


def test_halt_watchdog_and_daily_summary(s, sent):
    from perpdesk.store import heartbeat

    _link(s)
    st = _settings(alert_daily_hour=8)
    res = SimpleNamespace(halts={"trend_follow": "daily loss limit"}, halted=None)
    asyncio.run(notify.after_auto(s, None, None, st, NOW, res))
    asyncio.run(notify.after_auto(s, None, None, st, NOW + MIN, res))
    halts = [x for _, x in sent if "paused" in x]
    days = [x for _, x in sent if "Daily summary" in x]
    assert len(halts) == 1 and "Trend follower paused: daily loss limit" in halts[0]
    assert len(days) == 1 and "No signals is normal" in days[0]
    heartbeat(s, "tick", True, 1)
    s.commit()
    # heartbeats use the wall clock, so check against "now + 1h"
    assert asyncio.run(notify.watchdog(s, st, int(datetime.now(UTC).timestamp() * 1000) + 3_600_000)) == "tick stale"
    assert asyncio.run(notify.watchdog(s, st, int(datetime.now(UTC).timestamp() * 1000) + 3_700_000)) is None


# ---------------------------------------------------------------- webhook linking (full app)

@pytest.fixture()
def app_tg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/w.db")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
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
    with respx.mock(assert_all_called=False) as router:
        fakes.install(router)
        calls = []

        def tg_api(request):
            calls.append((request.url.path.rsplit("/", 1)[-1], request.content.decode()))
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"username": "perpdesk_test_bot"}})
            return httpx.Response(200, json={"ok": True, "result": True})

        router.post(url__regex=r"https://api\.telegram\.org/bot.*").mock(side_effect=tg_api)
        from fastapi.testclient import TestClient

        yield TestClient(webapp.create_app()), calls
    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()


def test_connect_link_and_commands(app_tg):
    c, calls = app_tg
    page = c.get("/settings").text
    assert "Connect Telegram" in page and TOKEN not in page
    r = c.post("/telegram/connect", follow_redirects=False)
    assert r.status_code == 303 and "Telegram%20refused" not in r.headers["location"]
    hook = next(body for name, body in calls if name == "setWebhook")
    st = Settings(telegram_bot_token=TOKEN)
    assert f"/api/telegram/{tg.webhook_secret(st)}" in hook
    page = c.get("/settings").text
    assert "t.me/perpdesk_test_bot?start=" in page
    code = page.split("?start=")[1].split('"')[0]
    url = f"/api/telegram/{tg.webhook_secret(st)}"
    assert c.post("/api/telegram/wrong", json={}).status_code == 403
    stranger = {"message": {"from": {"id": 99}, "chat": {"id": 99}, "text": "/status"}}
    n = len(calls)
    assert c.post(url, json=stranger).status_code == 200 and len(calls) == n  # strangers get nothing
    assert c.post(url, json={"message": {"from": {"id": 7}, "chat": {"id": 70}, "text": "/start bad"}}).status_code == 200
    assert len(calls) == n
    c.post(url, json={"message": {"from": {"id": 7}, "chat": {"id": 70}, "text": f"/start {code}"}})
    assert "Linked" in calls[-1][1] and '"chat_id":70' in calls[-1][1].replace(" ", "")
    c.post(url, json={"message": {"from": {"id": 7}, "chat": {"id": 70}, "text": "/status"}})
    assert "Next 4h check" in calls[-1][1]
    c.post(url, json={"message": {"from": {"id": 7}, "chat": {"id": 70}, "text": "/summary"}})
    assert "Daily summary" in calls[-1][1]
    # a reused code does not link another chat
    c.post(url, json={"message": {"from": {"id": 8}, "chat": {"id": 80}, "text": f"/start {code}"}})
    assert '"chat_id":80' not in calls[-1][1].replace(" ", "")
    assert "linked" in c.get("/settings").text
    t = c.post("/telegram/test", follow_redirects=False)
    assert "Test%20message%20sent" in t.headers["location"]
    c.post("/telegram/unlink")
    assert "not linked" in c.get("/settings").text


def test_token_redacted_in_errors():
    from perpdesk.sources.base import _redact

    assert _redact(f"ConnectError: https://api.telegram.org/bot{TOKEN}/sendMessage") == \
        "ConnectError: https://api.telegram.org/bot***/sendMessage"


def test_limit_entry_alert_and_cancel_say_limit(s, sent):
    """The ZEC case: the breakout plan switched to a limit at 1174 while price was ~1190; it never filled."""
    st = _settings(alert_tz="+05:30")
    _link(s)
    t = _add(s, entry_type="limit", entry=1174.0, sl=1118.4, tp1=1249.5, tp2=1340.7, runaway=1229.6,
             expires_ms=NOW + 56 * MIN, client_id="zec")
    asyncio.run(notify.after_auto(s, None, None, st, NOW, None))
    text = sent[0][1]
    assert "LIMIT BUY at $1,174.00" in text and "don't chase" in text and "(market)" not in text
    assert "Cancel it if not filled by 14:31 +05:30" in text and "Valid until" not in text
    t.status, t.exit_reason = "cancelled", "not_triggered"
    s.commit()
    asyncio.run(notify.after_auto(s, None, None, st, NOW + 60 * MIN, None))
    assert "never came back to the $1,174.00 limit" in sent[1][1]


def test_verdict_keeps_limit_take_while_price_above_entry():
    from perpdesk.verdict import verdict

    v = {"price": 1190.0, "updated_at": datetime.fromtimestamp((NOW - 2 * MIN) / 1000, UTC).isoformat(),
         "direction": "LONG", "flags": [], "levels": [[1187.75, "support", "support ×52", 52]], "plan": {}}
    bot = SimpleNamespace(setup="breakout", direction="LONG", fill_px=None, entry=1174.0, stop_now=None, sl=1118.4,
                          tp1=1249.5, tp2=1340.7, exit_model="fixed", status="pending", created_ms=NOW - 5 * MIN,
                          note="", tp1_hit=False, entry_type="limit", expires_ms=NOW + 55 * MIN, runaway=1229.6,
                          coin="ZEC", take=True)
    vd = verdict(v, bot, NOW, Settings())
    assert vd.take and "LIMIT order at 1174" in vd.reason and vd.take_until_ms == NOW + 55 * MIN
    assert "resistance ×52" in vd.room.reason  # named by its side relative to the trade, not "support"
    assert not verdict(v, bot, NOW + 60 * MIN, Settings()).take
    assert "ran away" in verdict({**v, "price": 1231.0}, bot, NOW, Settings()).reason
