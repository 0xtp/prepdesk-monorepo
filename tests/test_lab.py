"""Strategy lab: strategies are causal, simulators follow the conservative rules, carry maths."""

import numpy as np
import pandas as pd
import pytest

from perpdesk.backtest.history import resample
from perpdesk.lab.backtest import run_strategy, summarize
from perpdesk.lab.sim import Fees, simulate_carry, simulate_trail
from perpdesk.lab.strategies import STRATEGIES, CarryConfig, Entry, carry_cost, daily_context

M5 = 300_000


def _path(days=360, seed=1):
    """5m candles: 150 days up, 60 days flat, 150 days down, with noise and occasional volume spikes."""
    rng = np.random.default_rng(seed)
    n = days * 288
    t = np.arange(n)
    trend = np.piecewise(t.astype(float), [t < 150 * 288, (t >= 150 * 288) & (t < 210 * 288), t >= 210 * 288],
                         [lambda x: 100 + x / 288 * 0.8, 220.0, lambda x: 220 - (x - 210 * 288) / 288 * 0.8])
    px = trend * (1 + 0.004 * np.sin(t / 97) + rng.normal(0, 0.0015, n))
    o = np.r_[px[0], px[:-1]]
    vol = rng.uniform(80, 120, n) * np.where(rng.random(n) < 0.01, 4, 1)
    idx = pd.DatetimeIndex(pd.to_datetime(1_600_000_000_000 + t * M5, unit="ms", utc=True))
    return pd.DataFrame({"open": o, "high": np.maximum(o, px) * 1.001, "low": np.minimum(o, px) * 0.999, "close": px,
                         "volume": vol}, index=idx)


@pytest.fixture(scope="module")
def data():
    c5 = _path()
    return c5, resample(c5, "4h"), resample(c5, "1d")


def test_trend_follow_trades_both_sides_and_profits_on_clean_trends(data):
    c5, c4, d1 = data
    tr = run_strategy(STRATEGIES["trend_follow"], "X", c4, d1, d1, c5, pd.DataFrame(), 0)
    dirs = {t.direction for t in tr}
    assert dirs == {"LONG", "SHORT"} and sum(t.r for t in tr) > 0
    assert all(t.exit_reason in ("trail", "stop", "timeout") for t in tr)
    s = summarize(tr)["trend_follow"]
    assert s["all"]["n"] == len(tr) and set(s["direction"]) == {"LONG", "SHORT"}


def test_breakdown_short_only_in_downtrend(data):
    c5, c4, d1 = data
    tr = run_strategy(STRATEGIES["breakdown_short"], "X", c4, d1, d1, c5, pd.DataFrame(), 0)
    assert tr and all(t.direction == "SHORT" for t in tr)
    down_start = int(c5.index[210 * 288].value // 1_000_000)
    assert all(t.signal_ms > down_start for t in tr)


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_strategies_are_causal(data, name):
    """The entry decided at bar k must be identical whether or not later bars exist (or are altered)."""
    _, c4, d1 = data
    s = STRATEGIES[name]
    full = s.prepare(c4, d1, d1)
    cut = len(c4) - 200
    future = c4.copy()
    future.iloc[cut + 1:, :] *= 1.5
    trunc = s.prepare(c4.iloc[:cut + 1], d1, d1)
    alt = s.prepare(future, d1, d1)
    for k in range(cut - 150, cut + 1):
        e_full = s.entry_at(list(full.itertuples(index=False))[k])
        assert e_full == s.entry_at(list(trunc.itertuples(index=False))[k])
        assert e_full == s.entry_at(list(alt.itertuples(index=False))[k])


def test_daily_context_uses_closed_days_only(data):
    _, c4, d1 = data
    ctx = daily_context(c4, d1, "d_")
    # the 4h bar closing at 04:00 on day D sees day D-1's close, not day D's
    k = 7  # some bar in the middle of a day
    t_close = c4.index[k] + pd.Timedelta(hours=4)
    seen = ctx["d_close"].iloc[k]
    closed = d1[d1.index + pd.Timedelta(days=1) <= t_close]
    assert (np.isnan(seen) and closed.empty) or seen == closed["close"].iloc[-1]


def _bars(prices, start=0):
    p = np.array(prices, float)
    o = np.r_[p[0], p[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, p), "low": np.minimum(o, p), "close": p, "volume": 1.0},
                        index=pd.DatetimeIndex(pd.to_datetime([start + i * M5 for i in range(len(p))], unit="ms",
                                                              utc=True)))


def test_trailing_stop_ratchets_and_exits():
    e = Entry("LONG", 95.0, "trail", trail_mult=2.0, atr=1.0, max_hold_h=100)
    up = [100, 101, 103, 106, 110, 109, 107.9, 107.5]
    r = simulate_trail(e, 0, _bars(up), len(up) * M5, Fees(0, 0, 0))
    assert r.exit_reason == "trail" and r.exit_px == pytest.approx(108.0) and r.r == pytest.approx(8 / 5)
    wide = Entry("LONG", 95.0, "trail", trail_mult=10.0, atr=1.0, max_hold_h=100)
    stopped = simulate_trail(wide, 0, _bars([100, 99, 96, 94]), 4 * M5, Fees(0, 0, 0))
    assert stopped.exit_reason == "stop" and stopped.r == pytest.approx(-1.0)
    tight = simulate_trail(e, 0, _bars([100, 99, 96, 94]), 4 * M5, Fees(0, 0, 0))  # trail already at 98
    assert tight.exit_reason == "trail" and tight.r == pytest.approx(-0.4)
    short = simulate_trail(Entry("SHORT", 105, "trail", trail_mult=2, atr=1, max_hold_h=100), 0,
                           _bars([100, 98, 95, 96, 97.5]), 5 * M5, Fees(0, 0, 0))
    assert short.exit_reason == "trail" and short.exit_px == pytest.approx(97.0) and short.r > 0
    still = simulate_trail(e, 0, _bars([100, 101, 102]), 3 * M5, Fees(0, 0, 0))
    assert still.exit_reason == "open" and still.stop_now == pytest.approx(100.0)
    timeout = simulate_trail(Entry("LONG", 95, "trail", trail_mult=50, atr=1, max_hold_h=1), 0,
                             _bars([100.0] * 20), 20 * M5, Fees(0, 0, 0))
    assert timeout.exit_reason == "timeout"
    gap = simulate_trail(e, 0, _bars([94.0, 94.0]), 2 * M5, Fees(0, 0, 0))
    assert gap.exit_reason == "stop" and gap.r == -1.0


def test_funding_carry_enters_on_high_funding_and_pays_costs():
    c = CarryConfig()
    hours = 24 * 10
    rates = np.r_[np.full(48, 0.000005), np.full(hours, 0.00005), np.full(72, 0.000001)]
    idx = pd.DatetimeIndex(pd.to_datetime(np.arange(len(rates)) * 3_600_000, unit="ms", utc=True))
    tr = simulate_carry(pd.Series(rates, index=idx), c)
    assert len(tr) == 1 and tr[0].end_ms is not None
    t = tr[0]
    assert t.funding > 0.00005 * (hours - 30) and t.net == pytest.approx(t.funding - carry_cost(c))
    assert t.return_on_capital == pytest.approx(t.net / c.capital_per_notional)
    assert simulate_carry(pd.Series(np.full(100, 0.000001), index=idx[:100])) == []


def test_room_status_recorded_with_level_book(data):
    from perpdesk.lab.room import LevelBook

    c5, c4, d1 = data
    book = LevelBook({"1h": resample(c5, "1h"), "4h": c4, "1d": d1, "1w": resample(c5, "1w")})
    tr = run_strategy(STRATEGIES["trend_follow"], "X", c4, d1, d1, c5, pd.DataFrame(), 0, book=book)
    assert tr and all(t.room in ("ok", "caution", "block", "n/a") for t in tr)
    s = summarize(tr)["trend_follow"]
    assert sum(b.get("n", 0) for b in s["room"].values()) == len(tr)
