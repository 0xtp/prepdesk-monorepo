import numpy as np
import pytest

from perpdesk.tracking import calibration as cal
from perpdesk.tracking.gates import GateConfig, evaluate, live_check
from perpdesk.tracking.stats import bootstrap_window_percentile, equity_curve, quarterly_expectancy, r_stats, wilson

Q = 91 * 86_400_000


def test_wilson_reference():
    lo, hi = wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)


def test_r_stats_basic():
    s = r_stats([1.0, -1.0, 2.0, -1.0, -1.0, 0.5])
    assert s.n == 6 and s.wins == 3
    assert s.avg_r == pytest.approx(0.0833, abs=1e-3)
    assert s.profit_factor == pytest.approx(3.5 / 3)
    assert s.max_losing_streak == 2
    assert s.max_dd_r == pytest.approx(2.0)
    assert r_stats([]).n == 0


def test_quarters_and_curve():
    q = quarterly_expectancy([0, Q, 2 * Q], [1.0, -1.0, 0.5])
    assert len(q) >= 2
    assert equity_curve([2, 1], [1.0, 2.0]) == [(1, 2.0), (2, 3.0)]


def _trades(n, p_win, win_r=2.0, seed=1):
    rng = np.random.default_rng(seed)
    wins = rng.random(n) < p_win
    rs = np.where(wins, win_r, -1.0)
    ts = np.linspace(0, 4 * Q, n).astype(int)
    return ts.tolist(), rs.tolist(), [win_r] * n, wins.tolist()


def test_gate_passes_strong_edge():
    ts, rs, rr, won = _trades(400, 0.5)
    res = evaluate(ts, rs, rr, won, GateConfig(max_dd_r=30))
    assert res.passed, res.reasons


def test_gate_fails_small_sample_and_no_edge():
    ts, rs, rr, won = _trades(50, 0.5)
    assert any("OOS trades" in r for r in evaluate(ts, rs, rr, won, GateConfig()).reasons)
    ts, rs, rr, won = _trades(400, 0.3)
    res = evaluate(ts, rs, rr, won, GateConfig())
    assert not res.passed and any("expectancy" in r for r in res.reasons)


def test_live_check_demotes():
    oos = [2.0, -1.0] * 100
    assert not live_check([1.0] * 10, oos, 30).demote  # too few live trades
    d = live_check([-1.0] * 40, oos, 30)
    assert d.demote and "expectancy" in (d.reason or "")
    assert bootstrap_window_percentile(oos, 30, 5) is not None


def test_isotonic_monotone_and_platt():
    rng = np.random.default_rng(3)
    conf = rng.integers(0, 100, 600)
    won = rng.random(600) < conf / 100
    c = cal.fit(conf.tolist(), won.tolist())
    assert c.kind == "isotonic"
    assert all(a <= b + 1e-12 for a, b in zip(c.ps, c.ps[1:], strict=False))
    p_lo, *_ = c.predict(10)
    p_hi, *_ = c.predict(90)
    assert p_hi > p_lo
    small = cal.fit(conf[:100].tolist(), won[:100].tolist())
    assert small.kind == "platt"
    assert small.predict(90)[0] > small.predict(10)[0]
    assert cal.fit([], []).kind == "constant"
    rt = cal.Calibrator.from_json(c.to_json())
    assert rt.predict(50) == c.predict(50)


def test_reliability_error():
    buckets, err = cal.reliability([0.6] * 100, [True] * 60 + [False] * 40)
    assert err == pytest.approx(0.0, abs=1e-9) and buckets[0]["n"] == 100
    _, err2 = cal.reliability([0.9] * 100, [True] * 50 + [False] * 50)
    assert err2 == pytest.approx(0.4)
