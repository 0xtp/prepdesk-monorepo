import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from perpdesk.config import Settings, effective_settings


def test_config_validation_and_helpers():
    s = Settings(_env_file=None)
    assert s.favorite_groups[0][0] == "Majors" and "BTC" in s.favorite_groups[0][1]
    assert sum(s.tf_weight_map.values()) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, risk_pct_of_margin=25)
    with pytest.raises(ValueError):
        Settings(_env_file=None, timeframes="1h,7m")
    with pytest.raises(ValueError):
        Settings(_env_file=None, execution_enabled=True)
    with pytest.raises(ValueError):
        Settings(_env_file=None, auth_enabled=True)
    pg = Settings(_env_file=None, database_url="postgres://u:p@h:6543/db")
    assert pg.database_url.startswith("postgresql+psycopg://")
    m = Settings(_env_file=None, anthropic_api_key="sk-ant-123456789").masked()
    assert m["anthropic_api_key"].startswith("••••") and "123456789" not in m["anthropic_api_key"]
    eff = effective_settings({"weights_trend": 0.5, "app_name": "hacked"})
    assert eff.weights_trend == 0.5 and eff.app_name != "hacked"
    assert Settings(_env_file=None, default_favorites="BTC,ETH").favorite_groups == [("Favourites", ["BTC", "ETH"])]


def test_explain_guard_and_deterministic():
    from perpdesk.llm.explain import deterministic, facts_from_view, guard_ok

    view = {"asset": {"symbol": "SOL", "perp": True}, "direction": "LONG", "grade": "B", "setup_label": "Trend pullback",
            "setup_note": "", "expected_move": "UP", "confidence": 64, "top_for": ["EMA position (1d): up"],
            "top_against": [], "flags": [], "take": False, "take_reason": "x",
            "plan": {"sl": 101.2, "tp1": 104.0, "tp2": 105.4, "entry_note": "Prefer limit", "invalidation": "1H close",
                     "leverage": 5, "rr_tp1": 1.9}}
    facts = facts_from_view(view)
    assert guard_ok("Stop sits at 101.2, first target 104.0. Take half at TP1.", facts)
    assert not guard_ok("Price could rally to 120 soon.", facts)
    assert "SOL LONG" in deterministic(view)


def test_usage_cost_and_summary(tmp_path, monkeypatch):
    from perpdesk.db.session import _factory, get_engine, migrate
    from perpdesk.llm import usage

    assert usage.cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(2.0 + 1.0)
    assert usage.cost_usd("unknown-model", 10, 10) == 0.0
    url = f"sqlite:///{tmp_path}/u.db"
    migrate(url)
    s = _factory(url)()
    usage.record(s, "claude-sonnet-5-5", "explain", SimpleNamespace(input_tokens=1000, output_tokens=200))
    s.commit()
    st = Settings(_env_file=None, llm_credit_usd=5.0, llm_monthly_budget_usd=0.001)
    out = usage.summary(s, st)
    assert out["all"]["calls"] == 1 and out["balance"]["remaining"] < 5.0
    assert usage.budget_exhausted(s, st)
    s.close()
    get_engine.cache_clear()
    _factory.cache_clear()


def test_service_gate_lookup_and_take_hint(tmp_path):
    from perpdesk.db.models import BacktestRun, PlanOutcome, PlanRow, SetupStat
    from perpdesk.db.session import _factory, migrate
    from perpdesk.tracking import calibration, service

    url = f"sqlite:///{tmp_path}/g.db"
    migrate(url)
    s = _factory(url)()
    st = Settings(_env_file=None, min_sample_for_stats=5)
    assert service.gate_info(s, st, "trend_pullback", "SOL", "Large", "LONG", 70).passed is False
    run = BacktestRun(coins=["SOL"], params={}, summary={}, report_html="")
    s.add(run)
    s.flush()
    cal = calibration.fit([60] * 50 + [80] * 50, [False] * 25 + [True] * 25 + [True] * 40 + [False] * 10)
    s.add(SetupStat(run_id=run.id, setup="trend_pullback", scope_kind="asset", scope="SOL", direction="LONG",
                    params={"ema": 20.0}, stats={"n": 150, "win_rate": 0.5, "avg_r": 0.3, "profit_factor": 1.6,
                                                  "max_dd_r": 8}, gate_passed=True, gate_reasons=[],
                    calibration=cal.to_json()))
    s.flush()
    g = service.gate_info(s, st, "trend_pullback", "SOL", "Large", "LONG", 80)
    assert g.passed and g.stats is not None and g.proof and "150 OOS trades" in g.proof
    assert service.take_hint(s, st, "A").take
    assert not service.take_hint(s, st, "B").take
    for i in range(6):
        p = PlanRow(coin="SOL", asset_class="Large", created_ms=i, expires_ms=i + 1, trading_tf="1h", direction="LONG",
                    grade="B", setup="trend_pullback", confidence=70, expected_move="UP", entry_type="limit", entry=1,
                    entry_market=1, zone_lo=1, zone_hi=1, sl=0.9, tp1=1.2, tp2=1.3, runaway=1.1, rr_tp1=2, risk_usd=1,
                    notional=1, qty=1, leverage=1, win_prob=0.6, status="closed")
        s.add(p)
        s.flush()
        s.add(PlanOutcome(plan_id=p.id, filled=True, exit_reason="tp2", r_multiple=1.0, tp1_hit=True,
                          exit_ms=i + 1, scored_at=datetime.now(UTC)))
    s.flush()
    assert service.take_hint(s, st, "B").take
    buckets, err, n = service.calibration_status(s, st)
    assert n == 6 and err == pytest.approx(0.4)
    assert service.calibration_flag(s, st) is not None
    s.close()


def test_execution_noop():
    from perpdesk.execution import NoopExecutor, OrderIntent, get_executor

    ex = get_executor()
    assert isinstance(ex, NoopExecutor)
    oi = OrderIntent(1, "SOL", True, 1.0, 100, 99, 101, 102, 0.5, 2)
    assert oi.client_order_id.startswith("0x") and len(oi.client_order_id) == 34
    assert oi.client_order_id == OrderIntent(1, "SOL", True, 1.0, 100, 99, 101, 102, 0.5, 2).client_order_id
    res = asyncio.run(ex.place(oi))
    assert not res.accepted


def test_hl_helpers():
    from perpdesk.sources.hyperliquid import candles_frame, funding_frame, impact_bps

    df = candles_frame([{"t": 2, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3"},
                        {"t": 1, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3"}])
    from perpdesk.timeutil import index_ms

    assert list(index_ms(df)) == [1, 2]
    assert funding_frame([]).empty
    book = {"bids": [(99.0, 10.0)], "asks": [(101.0, 1.0), (102.0, 100.0)]}
    assert impact_bps(book, 50, "buy") is not None
    assert impact_bps(book, 10_000_000, "buy") is None


def test_dex_address_detection():
    from perpdesk.sources.dexscreener import looks_like_address

    assert looks_like_address("0x940181a94A35A4569E4529A3CDfB74e38FD98631")
    assert looks_like_address("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
    assert not looks_like_address("SOL")


def test_fmt_usd_negative_sign_first():
    from perpdesk.web.app import fmt_usd

    assert fmt_usd(-14.894) == "-$14.89" and fmt_usd(3.5) == "$3.50" and fmt_usd(-0.001) == "$0.00"
    assert fmt_usd(None) == "–" and fmt_usd(1234.5, 0) == "$1,234"
