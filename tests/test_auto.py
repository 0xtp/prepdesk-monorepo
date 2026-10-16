"""Phase 2 auto-trader (paper): sizing, signal → paper trade lifecycle, safety limits, kill switch."""

import numpy as np
import pandas as pd
import pytest

from helpers import make_analysis
from perpdesk.analysis.types import AssetMeta
from perpdesk.auto import engine, state
from perpdesk.auto.sizing import size_trade
from perpdesk.config import Settings
from perpdesk.plan import Costs, PlanConfig, RiskInputs, build_plan, per_unit_loss

H4 = 4 * 3_600_000
COSTS = Costs(0.00075, 0.00045, 0.0003, 0.0, 12)
META = AssetMeta("SOL", True, 2, 20, [], False, "Large")


# ---------------------------------------------------------------- sizing

def test_size_from_risk_and_leverage_only_as_needed():
    sz = size_trade("LONG", 100, 95, COSTS, 500, 0.5, 3, META)
    assert sz.ok and sz.leverage == 1 and sz.risk_usd == pytest.approx(2.5, rel=0.02)
    assert sz.qty * per_unit_loss(100, 95, COSTS, 1) <= 2.5 + 1e-9
    tight = size_trade("LONG", 100, 99.8, COSTS, 500, 0.5, 3, META)  # tight stop -> bigger size -> leverage
    assert tight.ok and tight.leverage == 2 and tight.margin <= 500
    capped = size_trade("LONG", 100, 99.8, COSTS, 500, 2, 3, META)  # cap binds: size shrinks, risk goes down
    assert capped.ok and capped.leverage == 3 and capped.notional <= 1500 + 1e-6 and capped.risk_usd < 10


def test_size_rejections():
    assert not size_trade("LONG", 100, 101, COSTS, 500, 0.5, 3, META).ok
    assert "minimum" in size_trade("LONG", 100, 50, COSTS, 50, 0.5, 3, META).reason
    near = size_trade("LONG", 100, 99.8, COSTS, 500, 2, 3, AssetMeta("X", True, 2, 3, [], False, "Mid"), liq_buffer=50)
    assert not near.ok and "liquidation" in near.reason


# ---------------------------------------------------------------- engine

@pytest.fixture()
def db(tmp_path, monkeypatch):
    from perpdesk.db import session as dbs
    from perpdesk.db.models import Asset

    url = f"sqlite:///{tmp_path}/a.db"
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()
    dbs.migrate(url)
    s = dbs._factory(url)()
    s.add(Asset(key="SOL", symbol="SOL", hl_coin="SOL", perp_available=True, sz_decimals=2, max_leverage=20,
                asset_class="Large", margin_tiers=[]))
    s.commit()
    yield s
    s.close()
    dbs.get_engine.cache_clear()
    dbs._factory.cache_clear()


def _settings(**kw):
    base = {"execution_mode": "paper", "auto_coins": ["SOL"], "auto_paper_equity": 500.0}
    return Settings(**{**base, **kw})


def _fake_signal(monkeypatch, setup="breakout", direction="LONG"):
    from perpdesk.pipeline import AssetResult

    an = make_analysis(direction=direction, price=100.0, atr=1.0, setup=setup, entry_type="market", tf="4h")
    plan = build_plan(an, META, RiskInputs(250, 5), PlanConfig(), None)
    calls = []

    async def fake(s, hl, a, ctx, settings, **kw):
        calls.append((settings.trading_timeframe, kw.get("setup_params")))
        return AssetResult(a, an, plan, {})

    monkeypatch.setattr(engine, "analyse_asset", fake)
    return plan, calls


def _bars(start_ms, closes):
    idx = pd.DatetimeIndex(pd.to_datetime([start_ms + i * 60_000 for i in range(len(closes))], unit="ms", utc=True))
    c = np.array(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.01, "low": np.minimum(o, c) - 0.01, "close": c,
                         "volume": 1.0}, index=idx)


async def test_paper_trade_lifecycle_tp2(db, monkeypatch):
    plan, calls = _fake_signal(monkeypatch)
    st = _settings()
    t0 = 1_800_000_000_000 - 1_800_000_000_000 % H4 + 5 * 60_000  # 5 min after a 4h close
    feed = {"bars": None}

    async def bars(hl, coin, start, now):
        return feed["bars"]

    monkeypatch.setattr(engine, "_bars", bars)
    feed["bars"] = _bars(t0 - 60_000, [100.0])
    r = await engine.run(db, None, None, st, t0)
    db.commit()
    assert r.opened and calls == [("4h", {"breakout": {"lookback": 20.0}})]
    from perpdesk.db.models import AutoTrade

    t = db.query(AutoTrade).one()
    assert t.status == "pending" and t.mode == "paper" and t.leverage <= 3 and t.client_id.startswith("paper:SOL:")
    assert t.risk_usd == pytest.approx(500 * 0.005, rel=0.03)
    # same 4h bar: evaluated once only
    r2 = await engine.run(db, None, None, st, t0 + 10 * 60_000)
    assert not r2.opened
    # price fills at market, runs through TP1 then TP2
    path = [100.0, 100.0] + list(np.linspace(100, plan.tp2 + 0.5, 60))
    feed["bars"] = _bars(t0, path)
    now = t0 + len(path) * 60_000 + 60_000
    r3 = await engine.run(db, None, None, st, now)
    db.commit()
    db.refresh(t)
    assert t.status == "closed" and t.exit_reason == "tp2" and t.tp1_hit and t.pnl_usd > 0 and r3.closed
    assert state.equity(db, st) == pytest.approx(500 + t.pnl_usd)
    from perpdesk.auto.report import build

    rep = build(db, st, now)
    assert rep["total"] == pytest.approx(t.pnl_usd) and rep["stats"]["n"] == 1 and len(rep["curve"]) == 1


async def test_no_trade_unless_breakout_long_and_late_signals_skipped(db, monkeypatch):
    st = _settings()
    t0 = 1_800_000_000_000 - 1_800_000_000_000 % H4
    _fake_signal(monkeypatch, setup="trend_pullback")
    assert not (await engine.run(db, None, None, st, t0 + 60_000)).opened
    _fake_signal(monkeypatch, direction="SHORT")
    assert not (await engine.run(db, None, None, st, t0 + H4 + 60_000)).opened
    _fake_signal(monkeypatch)
    late = await engine.run(db, None, None, st, t0 + 2 * H4 + 45 * 60_000)
    assert not late.opened and "min after the 4h close" in late.skipped[0]
    sig = await engine.run(db, None, None, _settings(execution_mode="signals"), t0 + 3 * H4 + 60_000)
    assert sig.mode == "signals" and not sig.opened


async def test_stopped_out_then_limits_halt_new_entries(db, monkeypatch):
    plan, _ = _fake_signal(monkeypatch)
    st = _settings(auto_daily_loss_pct=0.4, auto_max_drawdown_pct=50)
    t0 = 1_800_000_000_000 - 1_800_000_000_000 % H4 + 60_000
    feed = {"bars": _bars(t0 - 60_000, [100.0])}

    async def bars(hl, coin, start, now):
        return feed["bars"]

    monkeypatch.setattr(engine, "_bars", bars)
    await engine.run(db, None, None, st, t0)
    feed["bars"] = _bars(t0, [100.0, 100.0, plan.sl - 1, plan.sl - 1])
    r = await engine.run(db, None, None, st, t0 + 10 * 60_000)
    db.commit()
    assert r.closed and "sl" in r.closed[0] and r.halted and "daily loss" in r.halted
    assert state.day_halted(db, t0 + 10 * 60_000)
    nxt = await engine.run(db, None, None, st, t0 + H4)
    assert not nxt.opened  # still the same UTC day
    state.resume(db)
    st2 = _settings(auto_max_drawdown_pct=0.4, auto_daily_loss_pct=20)
    r2 = await engine.run(db, None, None, st2, t0 + 2 * H4)
    assert r2.halted and "max drawdown" in r2.halted and not r2.opened


async def test_max_positions_and_kill_switch(db, monkeypatch):
    from perpdesk.db.models import Asset, AutoTrade

    db.add(Asset(key="ETH", symbol="ETH", hl_coin="ETH", perp_available=True, sz_decimals=2, max_leverage=20,
                 asset_class="Majors", margin_tiers=[]))
    db.commit()
    _fake_signal(monkeypatch)
    st = _settings(auto_coins=["SOL", "ETH"], auto_max_positions=1)
    t0 = 1_800_000_000_000 - 1_800_000_000_000 % H4 + 60_000

    async def bars(hl, coin, start, now):
        return _bars(t0 - 60_000, [100.0, 100.0, 101.0])

    monkeypatch.setattr(engine, "_bars", bars)
    r = await engine.run(db, None, None, st, t0)
    assert len(r.opened) == 1 and "positions already open" in r.skipped[0]
    await engine.run(db, None, None, st, t0 + 3 * 60_000)  # fill it
    db.commit()

    class U:
        ctxs = {"SOL": type("C", (), {"mark_px": 102.0})()}

    async def uni(hl):
        return U()

    import perpdesk.assets.registry as reg

    monkeypatch.setattr(reg, "universe", uni)
    out = await engine.kill(db, None, t0 + 5 * 60_000)
    db.commit()
    t = db.query(AutoTrade).one()
    assert t.status == "closed" and t.exit_reason == "killed" and t.exit_px == 102.0 and t.pnl_usd > 0 and out
    assert state.halted(db)["reason"] == "kill switch"
    assert (await engine.run(db, None, None, st, t0 + H4)).halted == "kill switch"
