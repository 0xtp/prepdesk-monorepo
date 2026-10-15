"""Yahoo Finance chart JSON (unofficial, best-effort): CME BTC futures (BTC=F), DXY, S&P 500.
Called directly (no yfinance dependency) to keep the serverless bundle small."""

from __future__ import annotations

import pandas as pd

from perpdesk.sources.base import request_json

NAME = "yahoo"
SYMBOLS = {"cme_btc": "BTC=F", "dxy": "DX-Y.NYB", "spx": "^GSPC"}


async def daily(symbol: str, range_: str = "1y") -> pd.DataFrame:
    d = await request_json(NAME, "GET", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                           params={"range": range_, "interval": "1d"}, retries=2)
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError(f"yahoo: no data for {symbol}")
    r = res[0]
    q = r["indicators"]["quote"][0]
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(r["timestamp"], dtype="int64"), unit="s", utc=True), name="ts")
    df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]}, index=idx)
    df = df.dropna(subset=["close"]).astype(float)
    # Normalise to session date at UTC midnight
    df.index = pd.DatetimeIndex(df.index).normalize()
    return df[~df.index.duplicated(keep="last")]
