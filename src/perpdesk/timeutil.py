"""Small typed helpers for timestamp conversions."""

from __future__ import annotations

import numpy as np
import pandas as pd


def index_ms(df: pd.DataFrame | pd.Series) -> np.ndarray:
    """UTC epoch milliseconds of a frame's DatetimeIndex."""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return np.asarray(idx.to_numpy(dtype="datetime64[ns]").astype("int64")) // 1_000_000
