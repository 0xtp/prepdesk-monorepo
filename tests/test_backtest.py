"""Backtester: no-lookahead guarantee, simulation and walk-forward plumbing (synthetic data, test-only)."""

import itertools

import numpy as np
import pandas as pd
import pytest

from perpdesk.analysis.engine import EngineConfig, analyse
from perpdesk.analysis.types import AssetMeta, MarketSnapshot
from perpdesk.backtest.history import drop_incomplete, resample
from perpdesk.backtest.runner import CoinRun, Frames, pass1, simulate, walk_forward
from perpdesk.plan import PlanConfig
from synth import gbm_candles

META = AssetMeta("SOL", True, 2, 20, [], False, "Large")


def make_frames(c1h: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {"1h": c1h}
    for tf in ("4h", "1d", "1w"):
        out[tf] = drop_incomplete(resample(c1h, tf), tf, "1h", c1h)
    return out


def _signature(a):
    return (a.direction, round(a.score, 12), a.confidence, a.expected_move, a.setup.name, len(a.levels),
            tuple((f.name, f.tf, round(f.score, 12)) for f in a.factors), round(a.atr, 12))


def _analyse_at(fr: Frames, t_ms: int, price: float):
    c = fr.asof(t_ms)
    snap = MarketSnapshot("SOL", pd.Timestamp(t_ms, unit="ms", tz="UTC"), price, META, c, current_funding=1e-5)
    return analyse(snap, EngineConfig())


@pytest.mark.parametrize("offset_h", [0, 1, 2, 3, 13])  # bar closes at different points inside 4h / 1d bars
def test_no_lookahead_truncated_vs_full_vs_perturbed(offset_h):
    base = gbm_candles(24 * 330, "1h", drift=0.0001, vol=0.01, seed=21)
    i = 24 * 300 + offset_h
    t_ms = int(base.index[i].value // 1_000_000) + 3_600_000  # close of bar i
    price = float(base["close"].iloc[i])
    a_full = _analyse_at(Frames.build(make_frames(base)), t_ms, price)
    a_trunc = _analyse_at(Frames.build(make_frames(base.iloc[: i + 1])), t_ms, price)
    rng = np.random.default_rng(5)
    future = base.copy()
    k = len(future) - (i + 1)
    shock = rng.normal(0, 0.05, k)
    for col in ("open", "high", "low", "close"):
        future.iloc[i + 1 :, future.columns.get_loc(col)] *= np.exp(shock)
    future.iloc[i + 1 :, future.columns.get_loc("volume")] *= 10
    a_pert = _analyse_at(Frames.build(make_frames(future)), t_ms, price)
    assert _signature(a_full) == _signature(a_trunc) == _signature(a_pert)


def test_asof_excludes_forming_higher_tf_bars():
    base = gbm_candles(24 * 40, "1h", seed=3)
    fr = Frames.build(make_frames(base))
    t_ms = int(base.index[24 * 30 + 2].value // 1_000_000) + 3_600_000  # 03:00 UTC: inside a 4h bar
    c = fr.asof(t_ms)
    last4 = int(c["4h"].index[-1].value // 1_000_000) + 4 * 3_600_000
    last1d = int(c["1d"].index[-1].value // 1_000_000) + 86_400_000
    assert last4 <= t_ms and last1d <= t_ms


def test_pass1_simulate_walk_forward_end_to_end():
    c5 = gbm_candles(12 * 24 * 420, "5m", drift=0.00002, vol=0.002, seed=8)
    c1h = resample(c5, "1h")
    pcfg = PlanConfig()
    recs = pass1("SOL", "Large", META, make_frames(c1h), None, None, None, None, EngineConfig(), pcfg, stride=3)
    assert recs and all(r.plans.get("composite") for r in recs)
    run = CoinRun("SOL", "Large", recs, c5, {}, 0.0, recs[0].t_ms, recs[-1].t_ms)
    trades = simulate(run, "composite", 0, pcfg)
    assert trades
    # one position at a time: each trade starts after the previous one ended
    for a, b in itertools.pairwise(trades):
        assert b.t_ms >= a.end_ms
    for t in trades:
        if t.filled and t.r is not None:
            assert t.r >= -1.0 - 1e-9  # never lose more than the planned risk
    wf, labelled = walk_forward([run], pcfg, train_months=3, test_months=2, holdout_months=1)
    assert wf.windows and wf.combos_tried > 0
    periods = {t.period for t in labelled}
    assert "oos" in periods
    oos_ts = [t.t_ms for t in labelled if t.period == "oos"]
    assert min(oos_ts) >= wf.windows[0][1]
    assert all(t.t_ms >= wf.holdout_start for t in labelled if t.period == "holdout")


# ---------------------------------------------------------------- data fixes: volume splice, 1m re-check

def _ohlcv(start_ms, n, step, vol, px=100.0):
    idx = pd.DatetimeIndex(pd.to_datetime([start_ms + i * step for i in range(n)], unit="ms", utc=True))
    return pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px, "volume": vol}, index=idx)


def test_binance_volume_rescaled_to_hyperliquid_on_overlap():
    from perpdesk.backtest.history import _merge, volume_scale

    h = 3_600_000
    bn = _ohlcv(0, 200, h, 1000.0)
    hl = _ohlcv(100 * h, 100, h, 50.0)
    assert volume_scale(bn, hl) == pytest.approx(0.05)
    out, desc = _merge(bn, hl)
    assert len(out) == 200 and (out["volume"] == 50.0).all() and "×0.05" in desc
    # too little overlap: leave volumes as they are
    out2, desc2 = _merge(bn.iloc[:110], _ohlcv(105 * h, 10, h, 50.0))
    assert out2["volume"].iloc[0] == 1000.0 and "×" not in desc2


def test_scorer_flags_ambiguous_exit_and_refine_uses_1m():
    from perpdesk.backtest.runner import CoinRun, refine
    from perpdesk.tracking.scorer import PlanSpec, score

    m5 = 300_000
    spec = PlanSpec("LONG", "market", 100, 98, 102, 104, 101, 0, 10 * 3_600_000, 48 * 3_600_000, 0.0, 0.0, 0.0)
    # bar 0 fills at 100; bar 1 spans 97..103 (stop and TP1 in the same 5m candle) -> coarse says stop first
    c = pd.DataFrame({"open": [100.0, 100.0, 103.0], "high": [100.5, 103.0, 105.0], "low": [99.5, 97.0, 102.5],
                      "close": [100.0, 103.0, 104.5], "volume": 1.0},
                     index=pd.DatetimeIndex(pd.to_datetime([0, m5, 2 * m5], unit="ms", utc=True)))
    out = score(spec, c, 3 * m5)
    assert out.exit_reason == "sl" and out.ambiguous
    # 1-minute truth: TP1 first, then a dip that stops at break-even only after TP1, then TP2 in the next 5m bar
    fine = pd.DataFrame({"open": [100.0, 100.5, 102.0, 101.0, 99.0], "high": [100.6, 102.2, 102.3, 101.2, 99.5],
                         "low": [99.9, 100.4, 101.5, 100.8, 97.0], "close": [100.5, 102.0, 101.0, 99.0, 99.2],
                         "volume": 1.0},
                        index=pd.DatetimeIndex(pd.to_datetime([m5 + i * 60_000 for i in range(5)], unit="ms",
                                                              utc=True)))
    run = CoinRun("X", "Mid", [], c, {}, 0.0, 0, 0, fine=lambda a, b: pd.concat([c.iloc[:1], fine]))
    better = refine(run, spec, c, out, 3 * m5)
    assert better.tp1_hit and better.exit_reason == "be" and better.r_multiple > 0
    assert run.refined == 1 and run.flipped == 1
    # no 1m data: keep the coarse (conservative) result
    run2 = CoinRun("X", "Mid", [], c, {}, 0.0, 0, 0, fine=lambda a, b: None)
    assert refine(run2, spec, c, out, 3 * m5) is out and run2.refined == 0
