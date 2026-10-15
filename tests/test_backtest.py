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
