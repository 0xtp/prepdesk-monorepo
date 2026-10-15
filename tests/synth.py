"""Synthetic OHLCV generators. TEST-ONLY: never imported by application code."""
from __future__ import annotations

import numpy as np
import pandas as pd

from perpdesk.analysis.types import TF_MS


def gbm_candles(n: int, tf: str = "1h", start: str = "2024-01-01", s0: float = 100.0, drift: float = 0.0,
                vol: float = 0.01, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = s0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate(([s0], close[:-1]))
    wig = np.abs(rng.normal(0, vol / 2, n)) * close
    high = np.maximum(open_, close) + wig
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol / 2, n)) * close
    volume = rng.lognormal(10, 0.5, n)
    idx = pd.date_range(start, periods=n, freq=pd.Timedelta(milliseconds=TF_MS[tf]), tz="UTC", name="ts")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = pd.Timedelta(milliseconds=TF_MS[tf])
    out = df.resample(rule, origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return out
