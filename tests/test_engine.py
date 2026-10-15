import pandas as pd
import pytest

from perpdesk.analysis import factors as fx
from perpdesk.analysis.cme_gap import find_gaps
from perpdesk.analysis.engine import EngineConfig, analyse, combine
from perpdesk.analysis.indicators import add_features
from perpdesk.analysis.setups import PARAM_GRIDS, SETUPS, SetupContext, choose_setup
from perpdesk.analysis.types import AssetMeta, EventInfo, Factor, MarketSnapshot, SetupSignal
from synth import gbm_candles, resample

META = AssetMeta("SOL", True, 2, 20, [], False, "Large")


def frames(drift=0.0002, seed=7, n=24 * 400):
    base = gbm_candles(n, "1h", drift=drift, vol=0.01, seed=seed)
    return base, {"1h": base, "4h": resample(base, "4h"), "1d": resample(base, "1d"), "1w": resample(base, "1w")}


def snap(drift=0.0002, seed=7, **kw):
    base, c = frames(drift, seed)
    t = base.index[-1] + pd.Timedelta(hours=1)
    return MarketSnapshot("SOL", t, float(base.close.iloc[-1]), META, c, current_funding=0.0000125, **kw)


def test_analyse_outputs_are_bounded_and_complete():
    a = analyse(snap(), EngineConfig())
    assert a.direction in ("LONG", "SHORT")
    assert -1 <= a.score <= 1 and 0 <= a.confidence <= 100
    assert a.expected_move in ("UP", "DOWN", "SIDEWAYS")
    assert a.atr > 0 and a.levels and a.factors
    assert all(-2 <= f.score <= 2 for f in a.factors)
    assert len(a.top_for) <= 3 and len(a.top_against) <= 3


def test_uptrend_reads_long_downtrend_short():
    assert analyse(snap(drift=0.0008, seed=1), EngineConfig()).direction == "LONG"
    assert analyse(snap(drift=-0.0008, seed=1), EngineConfig()).direction == "SHORT"


def test_flags_event_liquidity_volatility():
    s = snap(events=[EventInfo("CPI", None, "High", "USD")], day_volume_usd=1000.0)  # type: ignore[arg-type]
    s.events[0].at = s.asof.to_pydatetime() + pd.Timedelta(hours=1)
    a = analyse(s, EngineConfig(max_atr_pctl=0))
    codes = {f.code for f in a.flags}
    assert {"event", "liquidity", "volatility"} <= codes


def test_not_enough_data_raises():
    base = gbm_candles(30)
    s = MarketSnapshot("X", base.index[-1], 100.0, META, {"1h": base})
    with pytest.raises(ValueError):
        analyse(s, EngineConfig())


def test_combine_weights_and_ablation():
    fs = [Factor("a", "trend", "1d", 2.0, ""), Factor("b", "momentum", "1h", -2.0, ""),
          Factor("c", "sentiment", "-", 1.0, "", scored=False)]
    g, contrib, score = combine(fs, EngineConfig())
    assert g["trend"] == 2.0 and "sentiment" not in g
    assert score == pytest.approx((0.30 * 1 - 0.15 * 1) / 0.45)
    a = analyse(snap(), EngineConfig(disabled_groups=frozenset({"trend", "momentum", "structure", "derivatives",
                                                                  "regime", "sentiment", "macro"})))
    assert a.near_zero and a.confidence <= 20


def test_btc_counter_trend_flag():
    s = snap(drift=0.0008, seed=1)
    base, btc = frames(drift=-0.001, seed=2)
    s.btc_candles = btc
    s.coin = "SOL"
    a = analyse(s, EngineConfig())
    if a.direction == "LONG":
        assert any(f.code == "btc_counter" for f in a.flags)


def test_derivative_factors():
    idx = pd.date_range("2026-01-01", periods=800, freq="1h", tz="UTC")
    f = pd.DataFrame({"rate": [0.0000125] * 799 + [0.0005], "premium": 0.0}, index=idx)
    z, mean = fx.funding_zscore(f, 0.0005)
    assert z > 2
    out = fx.derivatives_factors(f, 0.0005, 0.001, None, 100)
    fund = next(x for x in out if x.name == "Funding")
    assert fund.score < 0 and "crowded longs" in fund.reason
    oi = pd.DataFrame({"oi": [100.0, 110.0], "price": [10.0, 11.0]},
                      index=pd.DatetimeIndex([idx[0], idx[0] + pd.Timedelta(hours=2)]))
    q = fx.oi_price_quadrant(oi, 1)
    assert q is not None and q[0] == 1.0


def test_setups_detect_and_choose():
    base, c = frames(drift=0.0008, seed=1)
    fr = {k: add_features(v) for k, v in c.items()}
    ctx = SetupContext(fr["1h"].iloc[-300:], fr["4h"], fr["1d"], 0.0, float(fr["1h"]["atr"].iloc[-1]))
    for name, fn in SETUPS.items():
        for p in PARAM_GRIDS[name]:
            sig = fn(ctx, p)
            assert sig is None or sig.direction in ("LONG", "SHORT")
    sigs = [SetupSignal("trend_pullback", "LONG", "limit"), SetupSignal("breakout", "SHORT", "market")]
    chosen, notes = choose_setup(sigs, "LONG", False)
    assert chosen.name == "trend_pullback" and notes
    chosen, _ = choose_setup([], "SHORT", True)
    assert chosen.name == "composite" and chosen.note == "range trade"


def test_cme_gap_detection_and_fill():
    idx = pd.DatetimeIndex(pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"]).tz_localize("UTC"))
    cme = pd.DataFrame({"open": [100, 101, 110, 110], "close": [101, 102, 109, 111]}, index=idx)
    gaps = find_gaps(cme, None, 120.0)
    assert len(gaps) == 1 and gaps[0].gap_low == 102 and gaps[0].gap_high == 110 and not gaps[0].filled
    hourly = gbm_candles(200, start="2026-01-05", s0=101)
    filled = find_gaps(cme, hourly.assign(low=95.0), 120.0)
    assert filled[0].filled
    assert fx.cme_gap_factor(gaps) is not None


def test_percentile_confidence():
    from perpdesk.analysis.engine import percentile_confidence

    q = tuple(i / 100 for i in range(101))  # uniform 0..1
    assert percentile_confidence(0.0, q) == 0
    assert percentile_confidence(0.6, q) == 60
    assert percentile_confidence(5.0, q) == 100
    s = snap()
    a = analyse(s, EngineConfig(score_quantiles=q))
    assert a.confidence == round(abs(a.score) * 100) or a.near_zero
