"""Historical data for backtests.

Candles: Binance USDT-M archive for history older than Hyperliquid's ~5,000-bar window, Hyperliquid
candles inside it (Hyperliquid wins where both exist). Funding: Hyperliquid's full funding history.
Higher timeframes (4h/1d/1w) are resampled from 1h so every timeframe comes from one consistent series.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from perpdesk.analysis.types import TF_MS
from perpdesk.sources import feargreed, yahoo
from perpdesk.sources.base import close_client
from perpdesk.sources.binance_hist import binance_symbol, load_klines
from perpdesk.sources.hyperliquid import Hyperliquid, closed_only
from perpdesk.timeutil import index_ms

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    if df.empty:
        return df
    if tf == "1w":
        out = df.resample("W-MON", label="left", closed="left").agg(AGG)
    else:
        out = df.resample(pd.Timedelta(milliseconds=TF_MS[tf]), origin="epoch", label="left", closed="left").agg(AGG)
    return out.dropna(subset=["open", "close"])


def drop_incomplete(df: pd.DataFrame, tf: str, src_tf: str, src: pd.DataFrame) -> pd.DataFrame:
    """Drop resampled bars whose period is not fully covered by source bars (e.g. the forming last bar)."""
    if df.empty or src.empty:
        return df
    last_src_close = int(src.index[-1].value // 1_000_000) + TF_MS[src_tf]
    close = index_ms(df) + (7 * 86_400_000 if tf == "1w" else TF_MS[tf])
    return df[close <= last_src_close]


@dataclass
class History:
    coin: str
    c1h: pd.DataFrame
    c15m: pd.DataFrame
    c5m: pd.DataFrame
    funding: pd.DataFrame
    sources: dict[str, str] = field(default_factory=dict)

    def frames(self) -> dict[str, pd.DataFrame]:
        out = {"1h": self.c1h, "15m": self.c15m}
        for tf in ("4h", "1d", "1w"):
            out[tf] = drop_incomplete(resample(self.c1h, tf), tf, "1h", self.c1h)
        return out


def _merge(binance: pd.DataFrame, hl: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if hl.empty and binance.empty:
        return binance, "none"
    if hl.empty:
        return binance, "binance only"
    if binance.empty:
        return hl, f"hyperliquid only from {hl.index[0]:%Y-%m-%d}"
    cut = hl.index[0]
    out = pd.concat([binance[binance.index < cut], hl]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out, f"binance {binance.index[0]:%Y-%m-%d}→{cut:%Y-%m-%d}, hyperliquid {cut:%Y-%m-%d}→"


async def _hl_candles(hl: Hyperliquid, coin: str, tf: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    try:
        df = await hl.candles(coin, tf, start_ms, end_ms)
    except Exception:
        return pd.DataFrame()
    return closed_only(df, tf, end_ms)


async def _load(coin: str, start: date, end: date, cache_dir: Path) -> History:
    hl = Hyperliquid()
    end_ms = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp() * 1000) + 86_400_000
    end_ms = min(end_ms, int(datetime.now(UTC).timestamp() * 1000))
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
    sources: dict[str, str] = {}
    frames: dict[str, pd.DataFrame] = {}
    for tf in ("1h", "15m", "5m"):
        bn = await asyncio.to_thread(load_klines, coin, tf, start, end, cache_dir)
        hlc = await _hl_candles(hl, coin, tf, max(start_ms, end_ms - 5000 * TF_MS[tf]), end_ms)
        merged, desc = _merge(bn, hlc)
        merged = merged[(index_ms(merged) >= start_ms) & (index_ms(merged) < end_ms)]
        frames[tf], sources[tf] = merged, desc
    try:
        funding = await hl.funding_history(coin, start_ms, end_ms)
    except Exception:
        funding = pd.DataFrame(columns=["rate", "premium"])
    sources["funding"] = "hyperliquid" if not funding.empty else "unavailable"
    sources["binance_symbol"] = binance_symbol(coin)
    return History(coin, frames["1h"], frames["15m"], frames["5m"], funding, sources)


def load_history(coin: str, start: date, end: date, cache_dir: Path) -> History:
    async def run() -> History:
        try:
            return await _load(coin, start, end, cache_dir)
        finally:
            await close_client()

    return asyncio.run(run())


def load_context_history() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Fear & Greed full history and CME BTC futures daily bars (both best effort)."""

    async def run() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
        try:
            try:
                fg = await feargreed.history(0)
            except Exception:
                fg = None
            try:
                cme = await yahoo.daily("BTC=F", "5y")
            except Exception:
                cme = None
            return fg, cme
        finally:
            await close_client()

    return asyncio.run(run())
