"""Historical USDT-M futures klines from Binance's public archive (data.binance.vision).

Used only by the backtester for history older than Hyperliquid's 5,000-bar window. The live
api.binance.com endpoints return HTTP 451 from US datacenters, but the archive is reachable.
Files are cached on disk (GitHub Actions caches the directory between runs).
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import httpx
import pandas as pd

ARCHIVE = "https://data.binance.vision/data/futures/um"
# Hyperliquid coin -> Binance USDT-M symbol, where the names differ
SYMBOL_MAP = {"kPEPE": "1000PEPEUSDT", "kBONK": "1000BONKUSDT", "kSHIB": "1000SHIBUSDT", "kFLOKI": "1000FLOKIUSDT"}


def binance_symbol(coin: str) -> str:
    return SYMBOL_MAP.get(coin, f"{coin.upper()}USDT")


def _parse(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode()
    first = text.split("\n", 1)[0]
    header = 0 if first.startswith("open_time") else None
    df = pd.read_csv(io.StringIO(text), header=header)
    if header is None:
        df.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
                      "taker_buy_volume", "taker_buy_quote_volume", "ignore"][: len(df.columns)]
    ot = df["open_time"].astype("int64")
    # some archive files use microseconds
    unit: Literal["us", "ms"] = "us" if ot.iloc[0] > 10**14 else "ms"
    idx = pd.DatetimeIndex(pd.to_datetime(list(ot), unit=unit, utc=True), name="ts")
    return pd.DataFrame({c: df[c].astype(float).to_numpy() for c in ("open", "high", "low", "close", "volume")}, index=idx)


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def load_klines(coin: str, interval: str, start: date, end: date, cache_dir: Path,
                client: httpx.Client | None = None) -> pd.DataFrame:
    """Monthly archives for complete months, daily archives for the current month."""
    sym = binance_symbol(coin)
    cache_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    c = client or httpx.Client(timeout=60, follow_redirects=True)
    frames: list[pd.DataFrame] = []
    today = datetime.now(UTC).date()
    try:
        for y, m in _months(start, end):
            is_current = (y, m) == (today.year, today.month)
            if not is_current:
                frames.append(_fetch(c, cache_dir, f"{ARCHIVE}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{y}-{m:02d}.zip"))
            else:
                for d in pd.date_range(date(y, m, 1), min(end, today), freq="D"):
                    frames.append(_fetch(c, cache_dir, f"{ARCHIVE}/daily/klines/{sym}/{interval}/"
                                                        f"{sym}-{interval}-{d:%Y-%m-%d}.zip"))
    finally:
        if own:
            c.close()
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    return out[(out.index >= lo) & (out.index < hi)]


def load_days(coin: str, interval: str, days: list[date], cache_dir: Path,
              client: httpx.Client | None = None) -> pd.DataFrame:
    """Daily archive files for specific days only (used to re-check single trades on 1-minute candles)."""
    sym = binance_symbol(coin)
    cache_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    c = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        frames = [_fetch(c, cache_dir, f"{ARCHIVE}/daily/klines/{sym}/{interval}/{sym}-{interval}-{d:%Y-%m-%d}.zip")
                  for d in sorted(set(days))]
    finally:
        if own:
            c.close()
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _fetch(c: httpx.Client, cache_dir: Path, url: str) -> pd.DataFrame:
    path = cache_dir / url.rsplit("/", 1)[1]
    missing = path.with_suffix(".missing")
    if path.exists():
        return _parse(path.read_bytes())
    if missing.exists():
        return pd.DataFrame()
    r = c.get(url)
    if r.status_code == 404:
        if "/monthly/" in url or _old_daily(url):  # not listed then; recent daily files may just be unpublished
            missing.write_text("404")
        return pd.DataFrame()
    r.raise_for_status()
    path.write_bytes(r.content)
    return _parse(r.content)


def _old_daily(url: str) -> bool:
    """A daily file older than a few days that 404s will never appear (coin not listed that day)."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\.zip$", url)
    if not m:
        return False
    return (datetime.now(UTC).date() - date.fromisoformat(m.group(1))).days > 3
