"""Moonshot bucket (watch-only) and the extra trend-follower coins with their half-size period."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from perpdesk.config import Settings
from perpdesk.wallet import moonshot

ME = "0x" + "a" * 40
MAIN = "0x" + "b" * 40
NOW = int(datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp() * 1000)


def test_net_flow_types():
    assert moonshot.net_flow({"delta": {"type": "deposit", "usdc": "200"}}, ME) == 200
    assert moonshot.net_flow({"delta": {"type": "withdraw", "usdc": "50", "fee": "1"}}, ME) == -51
    into = {"delta": {"type": "subAccountTransfer", "usdc": "30", "user": MAIN, "destination": ME}}
    out = {"delta": {"type": "subAccountTransfer", "usdc": "30", "user": ME, "destination": MAIN}}
    assert moonshot.net_flow(into, ME) == 30 and moonshot.net_flow(out, ME) == -30
    assert moonshot.net_flow({"delta": {"type": "accountClassTransfer", "usdc": "9", "toPerp": True}}, ME) == 0


class FakeHL:
    def __init__(self):
        self.value, self.ledger, self.positions = 200.0, [], []

    async def clearinghouse_state(self, user):
        return {"marginSummary": {"accountValue": str(self.value)}, "assetPositions": self.positions}

    async def spot_state(self, user):
        return {"balances": [{"coin": "USDC", "total": "0"}]}

    async def ledger_updates(self, user, start):
        return [u for u in self.ledger if u["time"] >= start]

    async def open_orders(self, user):
        return []


@pytest.fixture()
def s(tmp_path):
    from perpdesk.db import session as dbs

    url = f"sqlite:///{tmp_path}/m.db"
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
    dbs.migrate(url)
    sess = dbs._factory(url)()
    yield sess
    sess.close()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()


def test_pnl_net_of_flows_topup_warning_and_alerts_once(s, monkeypatch):
    from perpdesk.alerts import telegram as tg

    sent = []

    async def fake_notify(sess, settings, text, html=False):
        sent.append(text)
        return True

    monkeypatch.setattr(tg, "notify", fake_notify)
    st = Settings(moonshot_wallet=ME, moonshot_budget_usd=200, watch_wallet=MAIN)
    hl = FakeHL()
    first = asyncio.run(moonshot.sync(s, hl, st, NOW))
    assert first["value"] == 200 and first["pnl"] == 0
    hl.value = 90.0  # lost 110
    asyncio.run(moonshot.sync(s, hl, st, NOW + 60_000))
    hl.value = 190.0  # ...then topped up 100 after the loss: still -110, not -10
    hl.ledger = [{"time": NOW + 90_000, "hash": "0x1", "delta": {"type": "deposit", "usdc": "100"}}]
    third = asyncio.run(moonshot.sync(s, hl, st, NOW + 120_000))
    assert third["pnl"] == pytest.approx(-110) and len(third["topups"]) == 1
    v = moonshot.view(s, st)
    assert any("breaks rule 2" in w for w in v["warnings"]) and any("Half the bucket" in w for w in v["warnings"])
    asyncio.run(moonshot.alerts(s, st, third))
    asyncio.run(moonshot.alerts(s, st, moonshot.view(s, st)["state"]))
    assert sum("half the bucket is gone" in x for x in sent) == 1 and sum("topped up" in x for x in sent) == 1


def test_same_address_as_main_is_refused(s):
    st = Settings(moonshot_wallet=MAIN, watch_wallet=MAIN)
    assert "separate" in asyncio.run(moonshot.sync(s, FakeHL(), st, NOW))["error"]


def test_doubled_and_no_stop_warnings(s):
    st = Settings(moonshot_wallet=ME, moonshot_budget_usd=100)
    hl = FakeHL()
    hl.value = 100.0
    asyncio.run(moonshot.sync(s, hl, st, NOW))
    hl.value = 230.0
    hl.positions = [{"position": {"coin": "SOL", "szi": "1", "entryPx": "100", "positionValue": "120",
                                  "unrealizedPnl": "20", "leverage": {"type": "cross", "value": 10}}}]
    asyncio.run(moonshot.sync(s, hl, st, NOW + 60_000))
    w = moonshot.view(s, st, main_value=900)["warnings"]
    assert any("doubled" in x for x in w) and any("No stop on: SOL" in x for x in w)
    assert any("20% of everything" in x for x in w)


def test_new_trend_coin_half_size_window():
    st = Settings()
    assert st.new_trend_coin("BNB", NOW) and not st.new_trend_coin("SOL", NOW)
    assert not st.new_trend_coin("BNB", int(datetime(2026, 12, 21, tzinfo=UTC).timestamp() * 1000))
    from perpdesk.alerts import notify

    t = SimpleNamespace(coin="BNB", direction="LONG", setup="trend_follow", exit_model="trail", entry=600.0, fill_px=None,
                        sl=570.0, tp1=0.0, tp2=0.0, leverage=2, created_ms=NOW, notional=0, note="")
    msg = notify.take_message(t, Settings(alert_account_usd=1000))
    assert "<b>HALF</b> your usual size" in msg and "(1% of $1,000, HALVED for a new coin = $5.00)" in msg


def test_extra_coins_only_run_the_trend_follower(monkeypatch):
    from perpdesk.auto import lab_paper

    seen = []

    async def fake_asset(s, hl, coin):
        seen.append(coin)
        return None

    monkeypatch.setattr("perpdesk.auto.engine._asset", fake_asset)
    monkeypatch.setattr(lab_paper.state, "last_eval", lambda s, k: 0)
    monkeypatch.setattr(lab_paper.state, "set_last_eval", lambda s, k, v: seen.append(k))
    res = SimpleNamespace(skipped=[], errors=[], opened=[])
    st = Settings(lab_coins=["SOL"], trend_extra_coins=["BNB"])
    fake_s = SimpleNamespace(commit=lambda: None, execute=None)
    monkeypatch.setattr(lab_paper, "_active", lambda s, b: set())
    now = (NOW // (4 * 3_600_000)) * 4 * 3_600_000 + 60_000
    asyncio.run(lab_paper.open_directional(fake_s, None, st, ["trend_follow", "breakdown_short"], now, res))
    assert "trend_follow:BNB" in seen and "breakdown_short:BNB" not in seen and "breakdown_short:SOL" in seen
