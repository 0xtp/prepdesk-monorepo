"""Indicator tests against hand-computed reference values and independent formulas."""

import numpy as np
import pandas as pd
import pytest

from perpdesk.analysis import indicators as ind
from synth import gbm_candles


def test_ema_seeded_with_sma_reference():
    s = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    e = ind.ema(s, 3)
    # seed = mean(1,2,3) = 2; alpha = 0.5 -> 3, 4, 5, ...
    assert np.isnan(e.iloc[1])
    assert e.iloc[2] == pytest.approx(2.0)
    assert e.iloc[3] == pytest.approx(3.0)
    assert e.iloc[9] == pytest.approx(9.0)


def test_sma():
    s = pd.Series([2.0, 4, 6, 8])
    assert ind.sma(s, 2).tolist()[1:] == [3.0, 5.0, 7.0]


def test_rsi_wilder_reference():
    # Hand computation: 14 alternating +1/-1 deltas after a seed -> avg gain = avg loss -> RSI 50
    vals = [10.0]
    for i in range(30):
        vals.append(vals[-1] + (1 if i % 2 == 0 else -1))
    r = ind.rsi(pd.Series(vals), 14)
    assert r.iloc[14] == pytest.approx(50.0, abs=4)
    up = ind.rsi(pd.Series(np.arange(1, 40, dtype=float)), 14)
    assert up.iloc[-1] == pytest.approx(100.0)
    flat = ind.rsi(pd.Series([5.0] * 30), 14)
    assert flat.iloc[-1] == pytest.approx(50.0)


def test_rsi_matches_independent_loop():
    df = gbm_candles(300, seed=11)
    c = df["close"].to_numpy()
    d = np.diff(c)
    g, l_ = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = g[:14].mean(), l_[:14].mean()
    for i in range(14, len(d)):
        ag = (ag * 13 + g[i]) / 14
        al = (al * 13 + l_[i]) / 14
    expected = 100 - 100 / (1 + ag / al)
    assert ind.rsi(df["close"], 14).iloc[-1] == pytest.approx(expected, rel=1e-9)


def test_atr_reference_and_true_range():
    h = pd.Series([10.0, 11, 12, 11])
    lo = pd.Series([9.0, 9.5, 10, 9])
    c = pd.Series([9.5, 10.5, 11.5, 9.5])
    tr = ind.true_range(h, lo, c)
    assert tr.tolist() == [1.0, 1.5, 2.0, 2.5]
    a = ind.atr(h, lo, c, 2)
    assert a.iloc[1] == pytest.approx(1.25)
    assert a.iloc[2] == pytest.approx(1.25 + 0.5 * (2.0 - 1.25))


def test_macd_identity():
    df = gbm_candles(200, seed=2)
    m = ind.macd(df["close"])
    assert (m["hist"] - (m["macd"] - m["signal"])).abs().max() < 1e-12


def test_bollinger_population_std():
    s = pd.Series([1.0, 2, 3, 4, 5])
    b = ind.bollinger(s, 5, 2)
    sd = np.std([1, 2, 3, 4, 5])
    assert b["upper"].iloc[-1] == pytest.approx(3 + 2 * sd)
    assert b["bandwidth"].iloc[-1] == pytest.approx(4 * sd / 3)


def test_adx_bounded_and_trending():
    df = gbm_candles(400, drift=0.004, vol=0.004, seed=5)
    a = ind.adx(df["high"], df["low"], df["close"])
    v = a["adx"].dropna()
    assert ((v >= 0) & (v <= 100)).all()
    assert a["pdi"].iloc[-1] > a["mdi"].iloc[-1]


def test_rolling_percentile():
    s = pd.Series(np.arange(100, dtype=float))
    p = ind.rolling_percentile(s, 50)
    assert p.iloc[-1] == pytest.approx(100.0)


def test_vwap_resets_daily():
    idx = pd.date_range("2024-01-01 22:00", periods=4, freq="1h", tz="UTC")
    df = pd.DataFrame({"high": [10.0, 10, 20, 20], "low": [10.0, 10, 20, 20], "close": [10.0, 10, 20, 20],
                       "volume": [1.0, 1, 1, 1]}, index=idx)
    v = ind.session_vwap(df)
    assert v.iloc[1] == pytest.approx(10.0)
    assert v.iloc[2] == pytest.approx(20.0)  # new UTC day


def test_volume_profile_poc():
    idx = pd.date_range("2024-01-01", periods=60, freq="1h", tz="UTC")
    close = np.r_[np.full(40, 100.0), np.linspace(90, 110, 20)]
    df = pd.DataFrame({"high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1.0}, index=idx)
    poc, vah, val = ind.volume_profile(df, bins=40)
    assert abs(poc - 100) < 1.0
    assert val <= poc <= vah


def test_features_causal_full_vs_truncated():
    df = gbm_candles(1200, seed=9)
    full = ind.add_features(df)
    cut = ind.add_features(df.iloc[:900])
    cols = [c for c in ind.FEATURE_COLUMNS]
    pd.testing.assert_frame_equal(full.iloc[:900][cols], cut[cols])
