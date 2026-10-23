"""Live auto-trading: sizing A/B, entry rules, order signing, and the whole order lifecycle against a fake exchange
(limit entry with attached stop → targets → TP1 → stop to entry → close; market entry; trailing stop; limits;
kill switch; dry run; Telegram /autostart /autostop /autokill)."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx

from perpdesk.alerts import telegram as tg
from perpdesk.config import Settings
from perpdesk.live import trader
from perpdesk.live.exchange import Account, HLExchange, LiveConfigError, Order, cloid, round_px

NOW = int(datetime(2026, 10, 23, 8, 5, tzinfo=UTC).timestamp() * 1000)
MIN = 60_000
KEY = "0x" + "1" * 64  # throwaway test key
SUB = "0x" + "c" * 40
MASTER = "0x" + "d" * 40


# ---------------------------------------------------------------- pure parts


def _st(**kw):
    base = {"execution_mode": "paper", "live_mode": "live", "auth_enabled": True, "ui_password_hash": "x",
            "session_secret": "y", "hl_agent_private_key": KEY, "hl_account_address": SUB, "alert_tz": "UTC",
            "telegram_bot_token": "123:abc"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_size_a_risk_and_b_notional():
    a = trader.live_size(_st(live_risk_usd=1.0), 100.0, 97.0, 2, 20)
    assert a.ok and a.qty == pytest.approx(0.32) and a.risk_usd <= 1.0 and a.notional == pytest.approx(32.0)
    assert a.leverage == 5  # 3% stop: liquidation ≥ 9% away even at 5x
    a2 = trader.live_size(_st(live_risk_usd=2.0), 100.0, 97.0, 2, 20)
    assert a2.qty == pytest.approx(0.64)  # double the risk, about double the size (rounded down)
    b = trader.live_size(_st(live_size_mode="notional", live_notional_usd=12), 100.0, 97.0, 2, 20)
    assert b.ok and b.notional == pytest.approx(12.0) and b.risk_usd == pytest.approx(0.12 * (3 + 197 * 0.00045))
    wide = trader.live_size(_st(live_risk_usd=1.0, live_max_leverage=3), 100.0, 80.0, 2, 20)
    assert wide.leverage == 1  # 20% stop: no leverage
    # $1 risk with a 15% stop = $6.6 position: raising it to $10 would mean $1.53 risk (> 1.5x) → skipped
    sk = trader.live_size(_st(live_risk_usd=1.0), 100.0, 85.0, 2, 20)
    assert not sk.ok and "$10 minimum" in sk.reason
    up = trader.live_size(_st(live_risk_usd=1.0), 100.0, 88.0, 2, 20)  # 12% stop: $8.3 → raised to $10.2
    assert up.ok and up.notional >= 10 and "raised" in up.note
    half = trader.live_size(_st(live_risk_usd=2.0), 100.0, 97.0, 2, 20, half=True)
    assert half.qty == pytest.approx(0.32) and "half" in half.note


def test_entry_plan():
    from types import SimpleNamespace as NS

    st = _st()
    lim = NS(direction="LONG", exit_model="fixed", stop_now=None, sl=97.0, entry_type="limit", fill_px=None,
             status="pending", entry=100.0)
    assert trader.entry_plan(lim, 103.0, st).kind == "limit"
    mk = NS(direction="LONG", exit_model="trail", stop_now=96.0, sl=95.0, entry_type="market", fill_px=100.0,
            status="open", entry=100.0)
    p = trader.entry_plan(mk, 100.5, st)
    assert p.ok and p.sl == 96.0 and p.px == pytest.approx(min(100.5 * 1.005, 100 + 0.3 * 4))
    ran = trader.entry_plan(mk, 101.5, st)  # 0.375R above the entry
    assert not ran.ok and "ran" in ran.reason
    short = NS(direction="SHORT", exit_model="fixed", stop_now=None, sl=105.0, entry_type="market", fill_px=None,
               status="pending", entry=100.0)
    sp = trader.entry_plan(short, 99.8, st)
    assert sp.ok and sp.px == pytest.approx(max(99.8 * 0.995, 100 - 1.5))


def test_rounding_and_cloid():
    assert round_px(1234.5678, 2) == 1234.6 and round_px(0.123456789, 0) == 0.12346 and round_px(112345.6, 5) == 112346
    assert cloid(1, "sl", 0) != cloid(1, "sl", 1) and len(cloid("x")) == 34


def test_settings_validation():
    with pytest.raises(ValueError):
        Settings(_env_file=None, hl_agent_private_key="my seed words here")
    with pytest.raises(ValueError):
        Settings(_env_file=None, live_bots="funding_carry")
    with pytest.raises(ValueError):
        Settings(_env_file=None, live_notional_usd=5)
    assert Settings(_env_file=None).breakout_short_take is True and Settings(_env_file=None).trend_max_positions == 6


def test_readiness_needs_auth_and_keys():
    assert "AUTH_ENABLED" in trader.readiness(_st(auth_enabled=False))
    assert "HL_AGENT_PRIVATE_KEY" in trader.readiness(_st(hl_agent_private_key=""))
    assert "paper" in trader.readiness(_st(execution_mode="signals"))
    assert trader.readiness(_st()) is None
    assert trader.readiness(_st(live_mode="dry_run", auth_enabled=False, hl_agent_private_key="")) is None


# ---------------------------------------------------------------- signing against a mocked exchange


def test_signed_order_recovers_to_the_agent_and_targets_the_sub_account():
    from eth_account import Account as Eth
    from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

    from perpdesk.sources import base
    from perpdesk.sources.hyperliquid import Hyperliquid

    base._client = None
    agent = Eth.from_key(KEY).address.lower()
    posted = []

    def info(request):
        body = httpx.Request("POST", "x", content=request.content).read()
        import json

        b = json.loads(body)
        if b["type"] == "userRole":
            return httpx.Response(200, json={"role": "agent", "data": {"user": MASTER}})
        if b["type"] == "subAccounts":
            return httpx.Response(200, json=[{"subAccountUser": SUB, "name": "bot"}])
        if b["type"] == "metaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                                                           {"name": "SOL", "szDecimals": 2, "maxLeverage": 20}]},
                                             [{}, {}]])
        return httpx.Response(400)

    def exchange(request):
        import json

        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"resting": {"oid": 1}}, "waitingForFill"]}}})

    with respx.mock(assert_all_called=False) as r:
        r.post("https://api.hyperliquid.xyz/info").mock(side_effect=info)
        r.post("https://api.hyperliquid.xyz/exchange").mock(side_effect=exchange)
        from perpdesk.sources.base import CACHE

        CACHE.clear()
        ex = HLExchange(_st(), Hyperliquid())
        out = asyncio.run(ex.place([Order("SOL", True, 0.3219, 150.123, cloid("a")),
                                    Order("SOL", False, 0.32, 135.0, cloid("b"), reduce_only=True, trigger_px=145.0)],
                                   grouping="normalTpsl"))
    assert out[0] == {"resting": {"oid": 1}} and ex.vault == SUB
    body = posted[0]
    assert body["vaultAddress"] == SUB and body["action"]["grouping"] == "normalTpsl"
    o1, o2 = body["action"]["orders"]
    assert o1["a"] == 1 and o1["s"] == "0.32" and o1["p"] == "150.12" and o1["t"] == {"limit": {"tif": "Gtc"}}
    assert o2["r"] is True and o2["t"]["trigger"] == {"isMarket": True, "triggerPx": "145", "tpsl": "sl"}
    signer = recover_agent_or_user_from_l1_action(body["action"], body["signature"], SUB, body["nonce"], None, True)
    assert signer.lower() == agent
    assert KEY[2:] not in str(body)
    base._client = None


def test_bad_config_is_refused():
    from perpdesk.sources.hyperliquid import Hyperliquid

    with pytest.raises(LiveConfigError):
        HLExchange(_st(hl_account_address=""), Hyperliquid())
    from eth_account import Account as Eth

    with pytest.raises(LiveConfigError):
        HLExchange(_st(hl_account_address=Eth.from_key(KEY).address), Hyperliquid())


# ---------------------------------------------------------------- lifecycle against a fake exchange


class FakeEx:
    agent = "0x" + "a" * 40
    account = SUB

    def __init__(self):
        self.value, self.free = 50.0, 50.0
        self.positions: dict[str, dict[str, float]] = {}
        self.orders: dict[str, dict] = {}  # cloid -> order
        self.fill_log: list[dict] = []
        self.placed: list[tuple[list[Order], str]] = []
        self.cancelled: list[str] = []
        self.mid = {"SOL": 100.0, "ETH": 2000.0}
        self.ioc_fill = True
        self.reject: set[str] = set()  # "sl" / "tp" / "entry"
        self.leverage: dict[str, int] = {}
        self.oids: dict[str, int] = {}

    async def prepare(self):
        return {"agent": self.agent, "vault": True}

    async def asset(self, coin):
        return ({"SOL": 5, "ETH": 1}[coin], {"SOL": 2, "ETH": 4}[coin], 20)

    async def account_state(self):
        return Account(self.value, self.free, {k: dict(v) for k, v in self.positions.items()})

    async def open_orders(self):
        return [{"coin": o.coin, "cloid": c, "sz": str(o.sz), "oid": self.oids[c]} for c, o in self.orders.items()]

    async def fills(self, start_ms):
        return [f for f in self.fill_log if f["time"] >= start_ms]

    async def mids(self):
        return dict(self.mid)

    async def place(self, orders, grouping="na"):
        self.placed.append((orders, grouping))
        out = []
        for o in orders:
            kind = "sl" if o.trigger_px is not None and o.tpsl == "sl" else "tp" if o.trigger_px is not None else "entry"
            if kind in self.reject:
                out.append({"error": f"{kind} rejected"})
                continue
            if o.tif == "Ioc":
                if not self.ioc_fill:
                    out.append({"error": "Order could not immediately match"})
                    continue
                px = self.mid[o.coin]
                self.fill(o.coin, o.sz if o.is_buy else -o.sz, px, o.reduce_only)
                out.append({"filled": {"totalSz": str(o.sz), "avgPx": str(px), "oid": 9}})
                continue
            self.orders[o.cloid] = o
            self.oids[o.cloid] = len(self.oids) + 1
            out.append("waitingForFill" if grouping == "normalTpsl" and o.reduce_only else {"resting": {"oid": 1}})
        return out

    async def cancel(self, items):
        for _, c in items:
            self.cancelled.append(c)
            self.orders.pop(c, None)
        return []

    async def cancel_oids(self, items):
        gone = {oid for _, oid in items}
        for c in [c for c in self.orders if self.oids[c] in gone]:
            self.cancelled.append(c)
            self.orders.pop(c)
        return []

    async def set_leverage(self, coin, lev, cross=False):
        self.leverage[coin] = lev

    # --- test helpers: simulate the market
    def fill(self, coin, signed_sz, px, reduce=False, pnl=0.0, t=None):
        cur = self.positions.get(coin, {"szi": 0.0, "entry": px})
        new = round(cur["szi"] + signed_sz, 8)
        if abs(new) < 1e-9:
            self.positions.pop(coin, None)
        else:
            self.positions[coin] = {"szi": new, "entry": cur["entry"] if reduce else px}
        self.fill_log.append({"coin": coin, "px": str(px), "sz": str(abs(signed_sz)), "time": t or NOW,
                              "closedPnl": str(pnl), "fee": "0.01"})

    def fill_order(self, cl, px=None, pnl=0.0, t=None):
        o = self.orders.pop(cl)
        self.fill(o.coin, o.sz if o.is_buy else -o.sz, px or o.trigger_px or o.px, o.reduce_only, pnl, t)


@pytest.fixture()
def s(tmp_path):
    from perpdesk.db import session as dbs
    from perpdesk.db.models import Asset

    url = f"sqlite:///{tmp_path}/l.db"
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
def msgs(monkeypatch):
    out = []

    async def fake_notify(sess, settings, text, html=False):
        out.append(text)
        return True

    monkeypatch.setattr(tg, "notify", fake_notify)
    return out


def _paper(s, **kw):
    from perpdesk.db.models import AutoTrade
    from perpdesk.store import utcnow

    base = dict(mode="paper", coin="SOL", asset_id=1, setup="breakout", direction="LONG", status="pending",
                signal_ms=NOW - 5 * MIN, created_ms=NOW - 4 * MIN, entry_type="limit", entry=99.0, sl=96.0,
                tp1=104.0, tp2=109.0, runaway=110.0, expires_ms=NOW + 60 * MIN, max_hold_ms=48 * 3_600_000, qty=1.0,
                leverage=2, notional=100.0, margin=50.0, risk_usd=3.0, entry_fee=0, exit_fee=0, slippage=0,
                client_id=f"p:{kw.get('coin', 'SOL')}:{kw.get('setup', 'breakout')}:{kw.get('created_ms', 0)}",
                tp1_hit=False, take=True, take_reason="fresh signal, no warnings", updated_at=utcnow())
    base.update(kw)
    t = AutoTrade(**base)
    s.add(t)
    s.commit()
    return t


def _run(s, st, ex, now=NOW):
    return asyncio.run(trader.run(s, None, st, now, ex=ex))


def _live_rows(s):
    from sqlalchemy import select

    from perpdesk.db.models import AutoTrade

    return list(s.execute(select(AutoTrade).where(AutoTrade.mode == "live")).scalars())


def test_off_until_started_and_nothing_without_take(s, msgs):
    st, ex = _st(), FakeEx()
    _paper(s)
    _run(s, st, ex)
    assert not ex.placed and not _live_rows(s)  # switched off
    trader.start(s, st, "test")
    _paper(s, coin="SOL", take=False, client_id="nt")
    assert trader.is_on(s)


def test_limit_lifecycle_tp1_breakeven_then_close(s, msgs):
    st, ex = _st(), FakeEx()
    p = _paper(s)
    trader.start(s, st, "test")
    _run(s, st, ex)
    (t,) = _live_rows(s)
    orders, grouping = ex.placed[0]
    assert grouping == "normalTpsl" and orders[0].tif == "Gtc" and orders[0].px == 99.0 and not orders[0].reduce_only
    assert orders[1].reduce_only and orders[1].trigger_px == 96.0 and orders[1].sz == orders[0].sz == t.qty
    assert t.qty == pytest.approx(0.32) and ex.leverage["SOL"] == 5 and t.status == "pending"
    assert "LIVE ORDER PLACED" in msgs[-1] and "Limit buy 0.32 SOL" in msgs[-1] and "Loss if stopped" in msgs[-1]
    _run(s, st, ex)  # nothing new: copied once
    assert len(ex.placed) == 1
    # the entry fills between runs; the attached stop becomes a live order
    st_ = t.exec_state
    ex.fill_order(st_["entry"], 99.0)
    _run(s, st, ex, NOW + 5 * MIN)
    s.refresh(t)
    assert t.status == "open" and t.fill_px == 99.0
    tp = ex.placed[-1][0]
    assert [o.tpsl for o in tp] == ["tp", "tp"] and tp[0].sz == pytest.approx(0.16) and tp[1].sz == pytest.approx(0.16)
    assert all(o.reduce_only for o in tp) and tp[0].trigger_px == 104.0 and tp[1].trigger_px == 109.0
    assert "LIVE FILLED" in msgs[-1]
    # TP1 fills → stop moves to entry: new stop first, then the old one is cancelled
    old_sl = t.exec_state["sl"]
    ex.fill_order(t.exec_state["tp1"], 104.0, pnl=0.8)
    _run(s, st, ex, NOW + 30 * MIN)
    s.refresh(t)
    assert t.tp1_hit and t.exec_state["sl_px"] == 99.0 and old_sl in ex.cancelled
    new_sl = ex.orders[t.exec_state["sl"]]
    assert new_sl.trigger_px == 99.0 and new_sl.sz == pytest.approx(0.16)
    assert "TP1 HIT" in msgs[-1] and "Stop moved to entry" in msgs[-1]
    # the rest is stopped at entry → closed, P&L from the fills, leftover TP2 cancelled
    ex.fill_order(t.exec_state["sl"], 99.0, pnl=0.0, t=NOW + 60 * MIN)
    _run(s, st, ex, NOW + 65 * MIN)
    s.refresh(t)
    assert t.status == "closed" and t.exit_reason == "breakeven" and t.pnl_usd == pytest.approx(0.8 - 0.03)
    assert t.exec_state["tp2"] in ex.cancelled and "LIVE CLOSED" in msgs[-1] and "+0.77 USD" in msgs[-1]
    assert p.status == "pending"  # the paper trade is untouched


def test_unfilled_limit_is_cancelled_at_expiry(s, msgs):
    st, ex = _st(), FakeEx()
    _paper(s, expires_ms=NOW + 30 * MIN)
    trader.start(s, st, "test")
    _run(s, st, ex)
    (t,) = _live_rows(s)
    _run(s, st, ex, NOW + 31 * MIN)
    s.refresh(t)
    assert t.status == "cancelled" and t.exec_state["entry"] in ex.cancelled and not ex.orders
    assert "LIVE ORDER CANCELLED" in msgs[-1]


def test_market_entry_places_stop_and_trails_with_the_paper_bot(s, msgs):
    st, ex = _st(), FakeEx()
    p = _paper(s, setup="trend_follow", entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=94.0,
               stop_now=94.0, tp1=0.0, tp2=0.0, exit_model="trail", client_id="tf")
    trader.start(s, st, "test")
    _run(s, st, ex)
    (t,) = _live_rows(s)
    entry, stop = ex.placed[0][0][0], ex.placed[1][0][0]
    assert entry.tif == "Ioc" and entry.px == pytest.approx(100.5) and stop.trigger_px == 94.0 and stop.reduce_only
    assert len(ex.placed) == 2  # no targets on a trailing trade
    assert t.status == "open" and "LIVE TRADE OPENED" in msgs[0] and "trails every 4h" in msgs[0]
    # the paper bot raises its trailing stop → the live stop follows
    p.stop_now = 97.5
    s.commit()
    _run(s, st, ex, NOW + 4 * 60 * MIN)
    s.refresh(t)
    assert t.exec_state["sl_px"] == 97.5 and ex.orders[t.exec_state["sl"]].trigger_px == 97.5
    assert "LIVE STOP TRAILED" in msgs[-1]
    # a looser paper stop is ignored (stops only tighten)
    p.stop_now = 96.0
    s.commit()
    n = len(ex.placed)
    _run(s, st, ex, NOW + 8 * 60 * MIN)
    assert len(ex.placed) == n
    # the paper bot exits (e.g. time) → the live position is closed at market
    p.status, p.exit_reason = "closed", "timeout"
    s.commit()
    _run(s, st, ex, NOW + 9 * 60 * MIN)
    s.refresh(t)
    assert t.status == "closed" and "SOL" not in ex.positions and "LIVE CLOSED" in msgs[-1]


def test_stale_orders_in_the_coin_are_cancelled_before_a_new_trade(s, msgs):
    st, ex = _st(), FakeEx()
    ex.orders["0xstale"] = Order("SOL", False, 1.0, 90.0, "0xstale", reduce_only=True, trigger_px=101.0)
    ex.oids["0xstale"] = 99
    _paper(s, entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="m")
    trader.start(s, st, "test")
    _run(s, st, ex)
    assert "0xstale" in ex.cancelled and "0xstale" not in ex.orders


def test_limit_skipped_when_price_is_already_past_the_stop(s, msgs):
    st, ex = _st(), FakeEx()
    ex.mid["SOL"] = 95.0
    _paper(s)
    trader.start(s, st, "test")
    _run(s, st, ex)
    assert not ex.placed and "beyond the stop" in msgs[-1]


def test_rejected_stop_closes_the_position_at_once(s, msgs):
    st, ex = _st(), FakeEx()
    ex.reject = {"sl"}
    _paper(s, entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="m")
    trader.start(s, st, "test")
    _run(s, st, ex)
    (t,) = _live_rows(s)
    assert t.status == "closed" and t.exit_reason == "no_stop" and "SOL" not in ex.positions
    assert any("CLOSED AT ONCE" in m for m in msgs)


def test_refused_attached_stop_pulls_the_limit_entry(s, msgs):
    st, ex = _st(), FakeEx()
    ex.reject = {"sl"}
    _paper(s)
    trader.start(s, st, "test")
    _run(s, st, ex)
    (t,) = _live_rows(s)
    assert t.status == "cancelled" and t.exec_state["entry"] in ex.cancelled and not ex.orders
    assert "attached stop was refused" in msgs[-1]


def test_failed_emergency_close_keeps_the_trade_open_and_alerts(s, msgs):
    st, ex = _st(), FakeEx()
    _paper(s, entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="m")
    trader.start(s, st, "test")
    orig = ex.place

    async def place(orders, grouping="na"):  # entry fills; the stop and the market close are both refused
        if orders[0].reduce_only:
            return [{"error": "refused"}]
        return await orig(orders, grouping)

    ex.place = place
    _run(s, st, ex)
    (t,) = _live_rows(s)
    assert t.status == "open" and "NO STOP — ACT NOW" in msgs[-1] and "SOL" in ex.positions
    ex.place = orig  # next run: the safety net puts the stop back
    _run(s, st, ex, NOW + 5 * MIN)
    s.refresh(t)
    assert ex.orders[t.exec_state["sl"]].trigger_px == 96.0


def test_paper_kill_switch_does_not_close_real_positions(s, msgs):
    st, ex = _st(), FakeEx()
    p = _paper(s, setup="trend_follow", entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=94.0,
               stop_now=94.0, tp1=0.0, tp2=0.0, exit_model="trail", client_id="tf")
    trader.start(s, st, "test")
    _run(s, st, ex)
    p.status, p.exit_reason = "closed", "killed"
    s.commit()
    _run(s, st, ex, NOW + 5 * MIN)
    assert "SOL" in ex.positions


def test_missing_stop_is_re_placed(s, msgs):
    st, ex = _st(), FakeEx()
    _paper(s, entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="m")
    trader.start(s, st, "test")
    _run(s, st, ex)
    (t,) = _live_rows(s)
    ex.orders.pop(t.exec_state["sl"])  # someone cancelled it on Hyperliquid
    _run(s, st, ex, NOW + 5 * MIN)
    s.refresh(t)
    assert ex.orders[t.exec_state["sl"]].trigger_px == 96.0 and "STOP RE-PLACED" in msgs[-1]


def test_limits_max_positions_daily_loss_and_floor(s, msgs):
    st, ex = _st(live_max_positions=1), FakeEx()
    ex.mid["ETH"] = 2000.0
    _paper(s, entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="a")
    _paper(s, coin="ETH", entry_type="market", status="open", fill_px=2000.0, entry=2000.0, sl=1940.0, client_id="b",
           created_ms=NOW - 3 * MIN)
    trader.start(s, st, "test")
    _run(s, st, ex)
    rows = {t.coin: t for t in _live_rows(s)}
    assert rows["SOL"].status == "open" and rows["ETH"].status == "cancelled" and rows["ETH"].exit_reason == "skipped"
    assert "Not taken live" in msgs[-1] and "max 1" in msgs[-1]
    # a −$3 day stops new entries
    rows["SOL"].status, rows["SOL"].pnl_usd, rows["SOL"].exit_ms = "closed", -3.2, NOW
    ex.positions.clear()
    s.commit()
    _paper(s, coin="SOL", entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="c",
           created_ms=NOW - 2 * MIN)
    _run(s, st, ex, NOW + MIN)
    assert trader.day_halted(s, NOW + MIN) and any("Daily loss stop" in m for m in msgs)
    assert len([t for t in _live_rows(s) if t.status == "open"]) == 0
    # account below the floor halts until /autostart
    st2, ex2 = _st(), FakeEx()
    ex2.value = 34.0
    s.execute(__import__("sqlalchemy").delete(__import__("perpdesk.db.models", fromlist=["AutoTrade"]).AutoTrade))
    s.commit()
    from perpdesk.store import kv_set

    kv_set(s, trader.DAY_HALT_KEY, None)
    _paper(s, entry_type="market", status="open", fill_px=100.0, entry=100.0, sl=96.0, client_id="d")
    _run(s, st2, ex2, NOW + 2 * MIN)
    assert trader.halted(s) and "below the $35 floor" in msgs[-1] and not ex2.placed


def test_stop_cancels_unfilled_entries_and_kill_closes_everything(s, msgs):
    st, ex = _st(), FakeEx()
    _paper(s)
    _paper(s, coin="ETH", entry_type="market", status="open", fill_px=2000.0, entry=2000.0, sl=1940.0,
           client_id="e", created_ms=NOW - 3 * MIN)
    trader.start(s, st, "test")
    _run(s, st, ex)
    rows = {t.coin: t for t in _live_rows(s)}
    out = asyncio.run(trader.stop(s, st, "test", ex=ex))
    s.commit()
    assert "stopped" in out and "1 unfilled entry cancelled" in out and "1 open position keeps" in out
    assert rows["SOL"].status == "cancelled" and rows["ETH"].status == "open" and "ETH" in ex.positions
    assert not trader.is_on(s)
    out = asyncio.run(trader.kill(s, st, "test", ex=ex))
    s.refresh(rows["ETH"])
    assert "Kill switch" in out and "ETH: closed" in out and not ex.positions and not ex.orders
    assert rows["ETH"].status == "closed" and rows["ETH"].exit_reason == "killed" and trader.halted(s)


def test_live_needs_auth_even_when_started(s, msgs):
    st = _st(auth_enabled=False, ui_password_hash="", session_secret="")
    assert "AUTH_ENABLED" in trader.start(s, st, "test") and not trader.is_on(s)


def test_duplicate_run_places_once(s, msgs):
    """Two runs at the same moment: the unique client_id claim lets only one place orders."""
    st, ex = _st(), FakeEx()
    _paper(s)
    trader.start(s, st, "test")
    from perpdesk.db.models import AutoTrade

    p = s.query(AutoTrade).filter_by(mode="paper").one()
    s.add(AutoTrade(**{**{c.name: getattr(p, c.name) for c in AutoTrade.__table__.columns if c.name != "id"},
                       "mode": "x", "client_id": f"live:{p.id}"}))  # the other run claimed it first
    s.commit()
    _run(s, st, ex)
    assert not ex.placed


def test_dry_run_says_what_it_would_do(s, msgs):
    st, ex = _st(live_mode="dry_run", hl_agent_private_key="", auth_enabled=False), FakeEx()

    class HL:
        async def all_mids(self):
            return {"SOL": 100.0}

    _paper(s)
    trader.start(s, st, "test")
    asyncio.run(trader.run(s, HL(), st, NOW))
    assert "DRY RUN — I WOULD PLACE" in msgs[-1] and "Limit buy 0.32 SOL" in msgs[-1] and "No order was sent" in msgs[-1]
    asyncio.run(trader.run(s, HL(), st, NOW + MIN))
    assert len(msgs) == 1 and not ex.placed
    assert "DRY RUN" in trader.status_text(s, st, NOW) and "SOL LONG" in trader.status_text(s, st, NOW)


def test_trend_cap_six(monkeypatch):
    from perpdesk.auto import lab_paper

    st = Settings(_env_file=None)
    assert lab_paper.max_positions(st, "trend_follow") == 6 and lab_paper.max_positions(st, "breakdown_short") == 3


# ---------------------------------------------------------------- Telegram and web controls


def test_telegram_start_stop_kill(tmp_path, monkeypatch):
    import fakes

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/w.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-test_token")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("LIVE_MODE", "dry_run")
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
        router.post(url__regex=r"https://api\.telegram\.org/bot.*").mock(
            side_effect=lambda req: (calls.append(req.content.decode()), httpx.Response(200, json={"ok": True}))[1])
        from fastapi.testclient import TestClient

        c = TestClient(webapp.create_app())
        c.get("/healthz")  # runs the migrations
        st = get_settings()
        url = f"/api/telegram/{tg.webhook_secret(st)}"
        from perpdesk.db.session import session_scope
        from perpdesk.store import kv_set

        with session_scope() as sess:
            kv_set(sess, tg.LINK_KEY, {"chat_id": 70, "user_id": 7})

        def say(text):
            c.post(url, json={"message": {"from": {"id": 7}, "chat": {"id": 70}, "text": text}})
            return calls[-1]

        assert "OFF" in say("/auto") and "DRY RUN" in say("/auto")
        assert "started" in say("/autostart")
        assert "ON" in say("/auto")
        assert "Send <b>/autokill yes</b>" in say("/autokill").replace("\\u003c", "<").replace("\\u003e", ">")
        assert "stopped" in say("/autostop")
        assert "killed" in say("/autokill yes")
        n = len(calls)
        c.post(url, json={"message": {"from": {"id": 99}, "chat": {"id": 99}, "text": "/autostart"}})
        assert len(calls) == n  # strangers can't start it
        page = c.get("/auto").text
        assert "Live auto-trading" in page and "DRY RUN" in page
        assert "Save live settings" in c.get("/settings").text
        r = c.post("/auto/live/start", follow_redirects=False)
        assert r.status_code == 303 and "started" in r.headers["location"]
        r = c.post("/auto/live/kill", data={"confirm": "no"}, follow_redirects=False)
        assert "not%20used" in r.headers["location"]
    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
