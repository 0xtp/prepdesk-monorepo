"""alternative.me Fear & Greed Index (daily). Attribution: data from alternative.me."""

from __future__ import annotations

import pandas as pd

from perpdesk.sources.base import request_json

NAME = "alternative.me"


async def history(limit: int = 0) -> pd.DataFrame:
    """limit=0 returns the full history (used by backtests); live uses a small limit."""
    d = await request_json(NAME, "GET", "https://api.alternative.me/fng/", params={"limit": limit, "format": "json"})
    rows = d.get("data") or []
    if not rows:
        return pd.DataFrame(columns=["value"], index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows)
    idx = pd.DatetimeIndex(pd.to_datetime(df["timestamp"].astype("int64"), unit="s", utc=True), name="ts")
    out = pd.DataFrame({"value": df["value"].astype(float).to_numpy(),
                        "label": df["value_classification"].to_numpy()}, index=idx)
    return out.sort_index()
