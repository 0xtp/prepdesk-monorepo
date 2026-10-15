"""Test-only builders for Analysis objects."""
from __future__ import annotations

import pandas as pd

from perpdesk.analysis.types import Analysis, Flag, Level, Pivot, RangeInfo, SetupSignal


def make_analysis(direction="LONG", price=100.0, atr=1.0, levels=None, pivots=None, confidence=70,
                  setup="composite", entry_type="limit", expected="UP", rng=None, flags=None, tf="1h",
                  near_zero=False) -> Analysis:
    ts = pd.Timestamp("2026-01-01T00:00:00Z")
    return Analysis(
        coin="TEST", asof=ts, price=price, trading_tf=tf, factors=[], group_scores={}, score=0.3 if direction == "LONG"
        else -0.3, direction=direction, confidence=confidence, expected_move=expected,
        setup=SetupSignal(setup, direction, entry_type), atr=atr, atr_pctl=50.0, levels=levels or [],
        pivots=pivots or [], range=rng, flags=flags or [], top_for=[], top_against=[], htf_direction=direction,
        near_zero=near_zero,
    )


__all__ = ["Flag", "Level", "Pivot", "RangeInfo", "make_analysis"]
