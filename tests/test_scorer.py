import pandas as pd
import pytest

from perpdesk.tracking.scorer import PlanSpec, score

T0 = 1_700_000_000_000


def bars(rows, start=T0, step=60_000):
    idx = pd.to_datetime([start + i * step for i in range(len(rows))], unit="ms", utc=True)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def spec(**kw):
    base = dict(direction="LONG", entry_type="limit", entry=100.0, sl=99.0, tp1=101.5, tp2=102.5, runaway=101.0,
                created_ms=T0, expires_ms=T0 + 3_600_000, max_hold_ms=48 * 3_600_000, entry_fee=0.0, exit_fee=0.0,
                slippage=0.0)
    base.update(kw)
    return PlanSpec(**base)


def test_clean_stop_is_minus_one_r():
    b = bars([(100.5, 100.6, 99.9, 100.2), (100.2, 100.3, 98.9, 99.0)])
    o = score(spec(), b, T0 + 10 * 60_000)
    assert o.filled and o.exit_reason == "sl" and o.r_multiple == pytest.approx(-1.0)


def test_same_candle_stop_and_target_assumes_stop():
    b = bars([(100.5, 100.6, 99.9, 100.2), (100.2, 102.0, 98.9, 101.0)])
    o = score(spec(), b, T0 + 10 * 60_000)
    assert o.exit_reason == "sl"


def test_fill_candle_cannot_take_profit():
    b = bars([(100.5, 103.0, 99.9, 102.9), (102.9, 103.0, 102.6, 102.8)])
    o = score(spec(), b, T0 + 10 * 60_000)
    assert o.filled and o.exit_reason == "tp2"  # targets only from the next candle on


def test_tp1_then_breakeven():
    b = bars([(100.5, 100.6, 99.9, 100.2), (100.2, 101.6, 100.1, 101.4), (101.4, 101.4, 99.95, 100.0)])
    o = score(spec(), b, T0 + 10 * 60_000)
    assert o.tp1_hit and o.exit_reason == "be"
    assert o.r_multiple == pytest.approx(0.5 * 1.5)


def test_tp1_and_tp2_full_win():
    b = bars([(100.5, 100.6, 99.9, 100.2), (100.2, 101.6, 100.1, 101.4), (101.4, 102.6, 101.3, 102.5)])
    o = score(spec(), b, T0 + 10 * 60_000)
    assert o.exit_reason == "tp2" and o.r_multiple == pytest.approx(0.5 * 1.5 + 0.5 * 2.5)


def test_not_triggered_by_runaway_and_expiry():
    b = bars([(100.5, 101.2, 100.3, 101.1)])
    assert score(spec(), b, T0 + 120_000).exit_reason == "not_triggered"
    b2 = bars([(100.5, 100.6, 100.3, 100.4)] * 3)
    assert score(spec(), b2, T0 + 180_000).exit_reason == "open"
    assert score(spec(expires_ms=T0 + 120_000), b2, T0 + 180_000).exit_reason == "not_triggered"


def test_market_fill_and_timeout_with_costs():
    b = bars([(100.0, 100.2, 99.8, 100.1), (100.1, 100.3, 99.9, 100.5)], step=3_600_000)
    s = spec(entry_type="market", max_hold_ms=3_600_000, entry_fee=0.0005, exit_fee=0.0005, slippage=0.0003)
    o = score(s, b, T0 + 3 * 3_600_000)
    assert o.exit_reason == "timeout" and o.fill_price == 100.0
    assert o.r_multiple is not None and o.r_multiple < 0.1  # costs eat the small gain


def test_short_mirror():
    s = spec(direction="SHORT", entry=100, sl=101, tp1=98.5, tp2=97.5, runaway=99)
    b = bars([(99.5, 100.1, 99.4, 99.8), (99.8, 101.1, 99.7, 101.0)])
    assert score(s, b, T0 + 10 * 60_000).exit_reason == "sl"


def test_mfe_mae_tracked():
    b = bars([(100.5, 100.6, 99.9, 100.2), (100.2, 101.0, 99.5, 100.0), (100.0, 100.1, 98.9, 99.0)])
    o = score(spec(), b, T0 + 10 * 60_000)
    assert o.mfe_r == pytest.approx(1.0) and o.mae_r <= -0.5
