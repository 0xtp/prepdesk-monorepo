import numpy as np
import pandas as pd

from perpdesk.analysis import structure as st
from perpdesk.analysis.types import Level, Pivot


def _df(highs, lows):
    idx = pd.date_range("2024-01-01", periods=len(highs), freq="1h", tz="UTC")
    return pd.DataFrame({"open": lows, "high": highs, "low": lows, "close": highs, "volume": 1.0}, index=idx)


def test_pivots_need_confirmation():
    highs = [1, 2, 3, 9, 3, 2, 1, 1]
    lows = [0.5] * 8
    df = _df(highs, lows)
    p = st.find_pivots(df, 3)
    assert any(x.kind == "high" and x.price == 9 for x in p)
    # with only 2 bars after the high it is not confirmed yet
    assert not any(x.kind == "high" for x in st.find_pivots(df.iloc[:6], 3))


def test_market_structure():
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    up = [Pivot(0, ts, 10, "high"), Pivot(1, ts, 5, "low"), Pivot(2, ts, 12, "high"), Pivot(3, ts, 6, "low")]
    assert st.market_structure(up)[0] == 1
    down = [Pivot(0, ts, 12, "high"), Pivot(1, ts, 6, "low"), Pivot(2, ts, 10, "high"), Pivot(3, ts, 5, "low")]
    assert st.market_structure(down)[0] == -1
    assert st.market_structure(up[:2])[0] == 0


def test_cluster_levels_labels():
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    pv = [Pivot(i, ts, p, "high") for i, p in enumerate([100, 100.4, 110, 90])]
    lv = st.cluster_levels(pv, 1.0, 105)
    kinds = {round(x.price): x.kind for x in lv}
    assert kinds[100] == "support" and kinds[110] == "resistance"
    assert max(x.strength for x in lv) == 2


def test_detect_range_and_touches():
    n = 60
    hi = np.where(np.arange(n) % 10 == 0, 110.0, 108.0)
    lo = np.where(np.arange(n) % 10 == 5, 100.0, 102.0)
    df = _df(hi, lo)
    r = st.detect_range(df, atr_value=2.0, lookback=48)
    assert r is not None and r.high == 110 and r.low == 100 and r.mid == 105
    assert st.detect_range(df, atr_value=0.5, lookback=48) is None  # too wide in ATR terms


def test_fvg_and_nearest():
    highs = [10, 11, 15, 16, 17]
    lows = [9, 10, 12, 15, 16]
    df = _df(highs, lows)
    gaps = st.fair_value_gaps(df)
    assert gaps and gaps[0].kind == "fvg"
    levels = [Level(95, "support"), Level(90, "support"), Level(110, "resistance")]
    assert st.nearest(levels, 100, "below")[0].price == 95
    assert st.nearest(levels, 100, "above", min_dist=20) == []


def test_equal_highs_untested():
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    df = _df([10, 10, 10, 10, 10, 10], [9] * 6)
    pv = [Pivot(1, ts, 10.0, "high"), Pivot(3, ts, 10.02, "high")]
    out = st.equal_extremes(pv, df, 0.05)
    assert out and out[0].kind == "eqh"
