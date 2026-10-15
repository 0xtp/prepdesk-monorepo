"""Technical indicators. All functions are causal: the value at bar t uses only bars <= t.

Conventions follow Wilder / TradingView defaults so values can be compared against charts:
EMA is seeded with the SMA of the first `n` values; RSI, ATR and ADX use Wilder smoothing
(alpha = 1/n) seeded with simple averages; Bollinger uses population std (ddof=0).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _seeded_recursive(values: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """out[n-1] = mean(values[:n]); out[t] = out[t-1] + alpha*(values[t]-out[t-1]). NaN before seed."""
    out = np.full(values.shape, np.nan)
    # skip leading NaNs
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size == 0:
        return out
    start = int(valid[0])
    if values.size - start < n:
        return out
    seed_end = start + n
    out[seed_end - 1] = float(np.mean(values[start:seed_end]))
    prev = out[seed_end - 1]
    for t in range(seed_end, values.size):
        v = values[t]
        if np.isnan(v):
            out[t] = prev
            continue
        prev = prev + alpha * (v - prev)
        out[t] = prev
    return out


def ema(s: pd.Series, n: int) -> pd.Series:
    vals = s.to_numpy(dtype=float)
    return pd.Series(_seeded_recursive(vals, n, 2.0 / (n + 1)), index=s.index)


def wilder(s: pd.Series, n: int) -> pd.Series:
    vals = s.to_numpy(dtype=float)
    return pd.Series(_seeded_recursive(vals, n, 1.0 / n), index=s.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = wilder(gain, n)
    avg_loss = wilder(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.where(avg_gain.notna())


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return wilder(true_range(high, low, close), n)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * sd
    lower = mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "bandwidth": (upper - lower) / mid})


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan
    atr_w = wilder(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(plus_dm, n) / atr_w
        mdi = 100 * wilder(minus_dm, n) / atr_w
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    dx = dx.replace([np.inf, -np.inf], np.nan)
    return pd.DataFrame({"adx": wilder(dx, n), "pdi": pdi, "mdi": mdi})


def rolling_percentile(s: pd.Series, window: int) -> pd.Series:
    """Percentile rank (0-100) of each value within its trailing window (inclusive)."""

    def _rank(a: np.ndarray) -> float:
        last = a[-1]
        if np.isnan(last):
            return np.nan
        v = a[~np.isnan(a)]
        return float((v <= last).sum() / v.size * 100)

    return s.rolling(window, min_periods=max(20, window // 5)).apply(_rank, raw=True)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP reset at each UTC day boundary; typical price weighted by volume."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    idx = pd.DatetimeIndex(df.index)
    day = idx.tz_convert("UTC").normalize() if idx.tz is not None else idx.normalize()
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum()
    out = cum_pv / cum_v.replace(0, np.nan)
    return out.fillna(tp)


def volume_profile(df: pd.DataFrame, bins: int = 40, value_area: float = 0.70) -> tuple[float, float, float]:
    """(POC, VAH, VAL) from bar typical prices weighted by volume."""
    if df.empty:
        raise ValueError("empty frame")
    lo = float(df["low"].min())
    hi = float(df["high"].max())
    if hi <= lo:
        return lo, hi, lo
    tp = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    hist, edges = np.histogram(tp, bins=bins, range=(lo, hi), weights=vol)
    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(np.argmax(hist))
    total = hist.sum()
    if total <= 0:
        return float(centers[poc_i]), hi, lo
    lo_i = hi_i = poc_i
    acc = hist[poc_i]
    while acc < value_area * total and (lo_i > 0 or hi_i < bins - 1):
        below = hist[lo_i - 1] if lo_i > 0 else -1.0
        above = hist[hi_i + 1] if hi_i < bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            acc += hist[hi_i]
        else:
            lo_i -= 1
            acc += hist[lo_i]
    return float(centers[poc_i]), float(edges[hi_i + 1]), float(edges[lo_i])


FEATURE_COLUMNS = (
    "ema20", "ema50", "ema200", "rsi", "macd", "macd_signal", "macd_hist", "atr", "atr_pctl",
    "bb_mid", "bb_upper", "bb_lower", "bb_bw", "bb_bw_pctl", "adx", "pdi", "mdi", "vol_ma20",
)


def add_features(df: pd.DataFrame, atr_pctl_window: int = 720) -> pd.DataFrame:
    """Append all indicator columns. Causal, so computing on a full history and slicing at t
    gives the same values as computing on data truncated at t (verified by tests)."""
    out = df.copy()
    c, h, low = out["close"], out["high"], out["low"]
    out["ema20"] = ema(c, 20)
    out["ema50"] = ema(c, 50)
    out["ema200"] = ema(c, 200)
    out["rsi"] = rsi(c, 14)
    m = macd(c)
    out["macd"], out["macd_signal"], out["macd_hist"] = m["macd"], m["signal"], m["hist"]
    out["atr"] = atr(h, low, c, 14)
    out["atr_pctl"] = rolling_percentile(out["atr"] / c, atr_pctl_window)
    b = bollinger(c)
    out["bb_mid"], out["bb_upper"], out["bb_lower"], out["bb_bw"] = b["mid"], b["upper"], b["lower"], b["bandwidth"]
    out["bb_bw_pctl"] = rolling_percentile(out["bb_bw"], 180)
    a = adx(h, low, c)
    out["adx"], out["pdi"], out["mdi"] = a["adx"], a["pdi"], a["mdi"]
    out["vol_ma20"] = sma(out["volume"], 20)
    return out


def ensure_features(df: pd.DataFrame) -> pd.DataFrame:
    if all(col in df.columns for col in FEATURE_COLUMNS):
        return df
    return add_features(df)
