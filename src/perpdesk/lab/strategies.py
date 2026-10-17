"""Lab strategies on 4h candles. Each `prepare` is causal (row t uses bars <= t only); daily and BTC
context is joined by the time those daily bars closed. `entry_at` turns one row into an entry or None.
Prices in an Entry are absolute; the trade fills at the next candle's open after the 4h close."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from perpdesk.analysis.indicators import adx, atr, bollinger, ema, rsi

DAY_MS = 86_400_000
H4_MS = 4 * 3_600_000


@dataclass(frozen=True)
class Entry:
    direction: Literal["LONG", "SHORT"]
    stop: float
    exit: Literal["fixed", "trail"]
    tp1: float = 0.0
    tp2: float = 0.0
    trail_mult: float = 0.0
    atr: float = 0.0
    max_hold_h: int = 48
    note: str = ""


class Strategy(Protocol):
    name: str
    label: str
    sides: str

    def prepare(self, c4h: pd.DataFrame, d1: pd.DataFrame, btc_d1: pd.DataFrame | None) -> pd.DataFrame: ...

    def entry_at(self, row: Any) -> Entry | None: ...


def daily_context(c4h: pd.DataFrame, d1: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Daily close / EMA20 / EMA50 as known at each 4h close (a daily bar counts only once it has closed)."""
    if d1 is None or d1.empty:
        return pd.DataFrame(index=c4h.index, columns=[f"{prefix}close", f"{prefix}ema20", f"{prefix}ema50"],
                        dtype=float)
    d = pd.DataFrame({f"{prefix}close": d1["close"], f"{prefix}ema20": ema(d1["close"], 20),
                      f"{prefix}ema50": ema(d1["close"], 50)})
    d.index = d.index + pd.Timedelta(milliseconds=DAY_MS)  # available from the daily close
    close_at = pd.DataFrame(index=c4h.index + pd.Timedelta(milliseconds=H4_MS))
    j = pd.merge_asof(close_at.reset_index(names="t"), d.reset_index(names="t").sort_values("t"), on="t",
                      direction="backward")
    j.index = c4h.index
    return j.drop(columns="t")


def _base(c4h: pd.DataFrame) -> pd.DataFrame:
    f = c4h[["open", "high", "low", "close", "volume"]].copy()
    f["atr14"] = atr(f["high"], f["low"], f["close"], 14)
    return f


class BreakdownShort:
    """4h close below the prior 20-bar low on above-average volume, while the coin's daily trend AND BTC's
    daily trend are down. Stop above the last 3 bars; 50% at 1.5R, stop to break-even, rest at 3R, 48h max."""

    name, label, sides = "breakdown_short", "Breakdown short", "SHORT"

    def prepare(self, c4h: pd.DataFrame, d1: pd.DataFrame, btc_d1: pd.DataFrame | None) -> pd.DataFrame:
        f = _base(c4h)
        f["low20"] = f["low"].shift(1).rolling(20, min_periods=20).min()
        f["vol20"] = f["volume"].shift(1).rolling(20, min_periods=20).mean()
        f["high3"] = f["high"].rolling(3, min_periods=3).max()
        f = f.join(daily_context(c4h, d1, "d_"))
        f = f.join(daily_context(c4h, btc_d1 if btc_d1 is not None else d1, "btc_"))
        return f

    def entry_at(self, r: Any) -> Entry | None:
        if not (r.close < r.low20 and r.volume > r.vol20 and r.d_close < r.d_ema50 and r.d_ema20 < r.d_ema50
                and r.btc_close < r.btc_ema50 and r.atr14 > 0):
            return None
        stop = max(r.high3 + 0.3 * r.atr14, r.close + 1.0 * r.atr14)
        dist = stop - r.close
        if dist > 3 * r.atr14:
            return None
        return Entry("SHORT", stop, "fixed", tp1=r.close - 1.5 * dist, tp2=r.close - 3 * dist, max_hold_h=48,
                     note="close below 20-bar low, coin and BTC in daily downtrends")


class TrendFollow:
    """Classic trend following, both sides: a fresh close beyond the 55-bar channel in the direction of the
    4h EMA50/EMA200 trend. Initial stop 2.5 ATR; chandelier trailing stop 3 ATR from the best price; 30 days max."""

    name, label, sides = "trend_follow", "Trend follower", "LONG+SHORT"

    def prepare(self, c4h: pd.DataFrame, d1: pd.DataFrame, btc_d1: pd.DataFrame | None) -> pd.DataFrame:
        f = _base(c4h)
        f["atr20"] = atr(f["high"], f["low"], f["close"], 20)
        f["hi55"] = f["high"].shift(1).rolling(55, min_periods=55).max()
        f["lo55"] = f["low"].shift(1).rolling(55, min_periods=55).min()
        f["prev_close"] = f["close"].shift(1)
        f["prev_hi55"] = f["hi55"].shift(1)
        f["prev_lo55"] = f["lo55"].shift(1)
        f["ema50"] = ema(f["close"], 50)
        f["ema200"] = ema(f["close"], 200)
        return f

    def entry_at(self, r: Any) -> Entry | None:
        if not r.atr20 > 0:
            return None
        if r.close > r.hi55 and r.prev_close <= r.prev_hi55 and r.ema50 > r.ema200:
            return Entry("LONG", r.close - 2.5 * r.atr20, "trail", trail_mult=3.0, atr=r.atr20, max_hold_h=720,
                         note="new 55-bar high in an uptrend")
        if r.close < r.lo55 and r.prev_close >= r.prev_lo55 and r.ema50 < r.ema200:
            return Entry("SHORT", r.close + 2.5 * r.atr20, "trail", trail_mult=3.0, atr=r.atr20, max_hold_h=720,
                         note="new 55-bar low in a downtrend")
        return None


class RangeRevert:
    """Mean reversion only in quiet markets (ADX < 20): buy a close below the lower Bollinger band with RSI < 30,
    sell a close above the upper band with RSI > 70. Stop 1.5 ATR; 50% at the middle band, rest at the opposite
    band; 48h max. Skipped if the middle band is less than 0.8R away."""

    name, label, sides = "range_revert", "Range trader", "LONG+SHORT"

    def prepare(self, c4h: pd.DataFrame, d1: pd.DataFrame, btc_d1: pd.DataFrame | None) -> pd.DataFrame:
        f = _base(c4h)
        f = f.join(bollinger(f["close"], 20, 2.0)[["mid", "upper", "lower"]])
        f["rsi14"] = rsi(f["close"], 14)
        f["adx14"] = adx(f["high"], f["low"], f["close"], 14)["adx"]
        return f

    def entry_at(self, r: Any) -> Entry | None:
        if not (r.adx14 < 20 and r.atr14 > 0):
            return None
        dist = 1.5 * r.atr14
        if r.close < r.lower and r.rsi14 < 30:
            if r.mid - r.close < 0.8 * dist:
                return None
            return Entry("LONG", r.close - dist, "fixed", tp1=r.mid, tp2=max(r.upper, r.mid + 0.5 * dist),
                         max_hold_h=48, note="below lower band in a quiet range")
        if r.close > r.upper and r.rsi14 > 70:
            if r.close - r.mid < 0.8 * dist:
                return None
            return Entry("SHORT", r.close + dist, "fixed", tp1=r.mid, tp2=min(r.lower, r.mid - 0.5 * dist),
                         max_hold_h=48, note="above upper band in a quiet range")
        return None


STRATEGIES: dict[str, Strategy] = {s.name: s for s in (BreakdownShort(), TrendFollow(), RangeRevert())}


# ---------------------------------------------------------------- funding carry (market-neutral)

@dataclass(frozen=True)
class CarryConfig:
    enter_hourly: float = 0.000015  # 24h average funding >= 0.0015%/h (~13% a year) to enter
    exit_hourly: float = 0.000008  # leave when the 24h average falls below 0.0008%/h (~7% a year)
    spot_fee: float = 0.0007  # taker, per side
    perp_fee: float = 0.00045
    slippage: float = 0.0003  # per leg per side
    capital_per_notional: float = 1.5  # spot (1.0) + perp margin at 2x (0.5)


def carry_rolling(rates: pd.Series) -> pd.Series:
    return rates.rolling(24, min_periods=24).mean()


def carry_cost(c: CarryConfig) -> float:
    """Round-trip cost as a fraction of notional: buy+sell spot, short+cover perp, slippage on all four."""
    return 2 * (c.spot_fee + c.perp_fee) + 4 * c.slippage


def carry_signal(avg24: float, in_position: bool, c: CarryConfig) -> bool:
    """True = should hold the carry position after this hour."""
    if np.isnan(avg24):
        return in_position
    return avg24 >= c.exit_hourly if in_position else avg24 >= c.enter_hourly
