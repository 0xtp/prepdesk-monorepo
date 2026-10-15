"""Factor functions. Each returns Factor objects with a score in [-2, +2] and a plain-English reason.

All inputs are frames already truncated to closed bars at the analysis time, with indicator
columns from `indicators.add_features`.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from perpdesk.analysis import structure as st
from perpdesk.analysis.indicators import sma, volume_profile
from perpdesk.analysis.types import CmeGap, EventInfo, Factor, Level, Pivot


def _clip(x: float, lim: float = 2.0) -> float:
    if not math.isfinite(x):
        return 0.0
    return float(max(-lim, min(lim, x)))


def _last(df: pd.DataFrame, col: str) -> float:
    v = df[col].iloc[-1]
    return float(v) if pd.notna(v) else float("nan")


def _fmt(x: float) -> str:
    return f"{x:,.6g}"


# ---------------------------------------------------------------- trend


def trend_factors(tf: str, df: pd.DataFrame) -> list[Factor]:
    out: list[Factor] = []
    if len(df) < 30:
        return out
    c = _last(df, "close")
    e20, e50, e200 = _last(df, "ema20"), _last(df, "ema50"), _last(df, "ema200")
    emas = [(20, e20), (50, e50), (200, e200)]
    have = [(n, v) for n, v in emas if math.isfinite(v)]
    if have:
        above = sum(1 for _, v in have if c > v)
        score = (above - len(have) / 2) / (len(have) / 2) * 1.5
        aligned = ""
        if len(have) == 3:
            if e20 > e50 > e200:
                score += 0.5
                aligned = ", EMAs stacked bullish"
            elif e20 < e50 < e200:
                score -= 0.5
                aligned = ", EMAs stacked bearish"
        names = "/".join(str(n) for n, _ in have)
        out.append(Factor("EMA position", "trend", tf, _clip(score),
                          f"price above {above} of {len(have)} EMAs ({names}){aligned}"))
    atrv = _last(df, "atr")
    if len(df) >= 56 and math.isfinite(atrv) and atrv > 0 and math.isfinite(e50):
        prev = float(df["ema50"].iloc[-6])
        slope = (e50 - prev) / atrv
        out.append(Factor("EMA50 slope", "trend", tf, _clip(slope * 1.5),
                          f"EMA50 moved {slope:+.2f} ATR over 5 bars"))
    return out


def weekly_ma_factor(weekly: pd.DataFrame) -> Factor | None:
    if len(weekly) < 50:
        return None
    c = float(weekly["close"].iloc[-1])
    ma50 = float(sma(weekly["close"], 50).iloc[-1])
    ma200_s = sma(weekly["close"], 200)
    ma200 = float(ma200_s.iloc[-1]) if pd.notna(ma200_s.iloc[-1]) else float("nan")
    d50 = (c / ma50 - 1) * 100
    if math.isfinite(ma200):
        d200 = (c / ma200 - 1) * 100
        score = (0.75 if c > ma50 else -0.75) + (0.75 if c > ma200 else -0.75)
        if d200 > 150:
            score -= 0.5  # historically extended vs the 200W MA
        reason = f"{d50:+.1f}% vs 50W MA, {d200:+.1f}% vs 200W MA"
    else:
        score = 1.0 if c > ma50 else -1.0
        reason = f"{d50:+.1f}% vs 50W MA (200W MA needs more history)"
    return Factor("50W/200W MA", "trend", "1w", _clip(score), reason)


def structure_trend_factor(tf: str, pivots: list[Pivot]) -> Factor | None:
    s, why = st.market_structure(pivots)
    if why == "not enough swings":
        return None
    return Factor("Market structure", "trend", tf, 1.5 * s, why)


# ---------------------------------------------------------------- momentum


def momentum_factors(tf: str, df: pd.DataFrame, pivots: list[Pivot]) -> list[Factor]:
    out: list[Factor] = []
    if len(df) < 35:
        return out
    r = _last(df, "rsi")
    if math.isfinite(r):
        if r >= 70:
            score, why = 0.5, f"RSI {r:.0f} overbought (strong but extended)"
        elif r <= 30:
            score, why = -0.5, f"RSI {r:.0f} oversold (weak but extended)"
        else:
            score, why = (r - 50) / 20, f"RSI {r:.0f}"
        div = rsi_divergence(df, pivots)
        if div:
            score += div
            why += ", bullish divergence" if div > 0 else ", bearish divergence"
        out.append(Factor("RSI", "momentum", tf, _clip(score), why))
    h = df["macd_hist"].dropna()
    if len(h) >= 4:
        h0, h1 = float(h.iloc[-1]), float(h.iloc[-2])
        if h0 > 0:
            score, why = (1.0, "MACD histogram positive and rising") if h0 > h1 else (0.3, "MACD histogram positive, fading")
        else:
            score, why = (-1.0, "MACD histogram negative and falling") if h0 < h1 else (-0.3, "MACD histogram negative, fading")
        recent = h.iloc[-4:]
        if (recent.iloc[:-1] <= 0).any() and h0 > 0:
            score += 0.5
            why += "; fresh bullish cross"
        elif (recent.iloc[:-1] >= 0).any() and h0 < 0:
            score -= 0.5
            why += "; fresh bearish cross"
        out.append(Factor("MACD", "momentum", tf, _clip(score), why))
    return out


def rsi_divergence(df: pd.DataFrame, pivots: list[Pivot]) -> float:
    """+1 regular bullish, -1 regular bearish, 0 none. Uses the last two confirmed pivots."""
    rs = df["rsi"].to_numpy(dtype=float)
    highs = [p for p in pivots if p.kind == "high"][-2:]
    lows = [p for p in pivots if p.kind == "low"][-2:]
    recent = len(df) - 15
    if len(highs) == 2 and highs[1].idx >= recent:
        a, b = highs
        if b.price > a.price and rs[b.idx] < rs[a.idx] - 2:
            return -1.0
    if len(lows) == 2 and lows[1].idx >= recent:
        a, b = lows
        if b.price < a.price and rs[b.idx] > rs[a.idx] + 2:
            return 1.0
    return 0.0


# ---------------------------------------------------------------- volatility (information; drives flags and plan)


def volatility_factors(tf: str, df: pd.DataFrame) -> list[Factor]:
    out: list[Factor] = []
    if len(df) < 30:
        return out
    ap = _last(df, "atr_pctl")
    atrv = _last(df, "atr")
    c = _last(df, "close")
    if math.isfinite(ap):
        out.append(Factor("ATR percentile", "volatility", tf, 0.0,
                          f"ATR {atrv:.4g} ({atrv / c * 100:.2f}% of price), {ap:.0f}th percentile", scored=False))
    bw = _last(df, "bb_bw_pctl")
    if math.isfinite(bw):
        why = "Bollinger squeeze (bandwidth in bottom 20%)" if bw <= 20 else f"Bollinger bandwidth {bw:.0f}th pct"
        out.append(Factor("Bollinger width", "volatility", tf, 0.0, why, scored=False))
    a = _last(df, "adx")
    if math.isfinite(a):
        regime = "trending" if a >= 25 else ("ranging" if a < 20 else "weak trend")
        out.append(Factor("ADX", "volatility", tf, 0.0, f"ADX {a:.0f} ({regime})", scored=False))
    return out


# ---------------------------------------------------------------- structure / levels


def levels_factors(tf: str, df: pd.DataFrame, levels: list[Level], atrv: float) -> list[Factor]:
    out: list[Factor] = []
    if df.empty or not math.isfinite(atrv) or atrv <= 0:
        return out
    c = float(df["close"].iloc[-1])
    sup = st.nearest(levels, c, "below", min_dist=0.05 * atrv)
    res = st.nearest(levels, c, "above", min_dist=0.05 * atrv)
    if sup and res:
        ds = (c - sup[0].price) / atrv
        dr = (res[0].price - c) / atrv
        score = (dr - ds) / (dr + ds) * 1.5
        out.append(Factor("Room to move", "structure", tf, _clip(score),
                          f"support {_fmt(sup[0].price)} ({ds:.1f} ATR below), resistance {_fmt(res[0].price)} "
                          f"({dr:.1f} ATR above)"))
    if "vwap" in df.columns:
        vw = float(df["vwap"].iloc[-1])
        out.append(Factor("Session VWAP", "structure", tf, 0.5 if c > vw else -0.5,
                          f"price {'above' if c > vw else 'below'} session VWAP {_fmt(vw)}"))
    if len(df) >= 50:
        poc, vah, val = volume_profile(df.iloc[-200:])
        if c > vah:
            score, why = 0.75, f"above value area (VAH {_fmt(vah)}), acceptance higher"
        elif c < val:
            score, why = -0.75, f"below value area (VAL {_fmt(val)}), acceptance lower"
        else:
            score, why = (0.25 if c > poc else -0.25), f"inside value area, {'above' if c > poc else 'below'} POC {_fmt(poc)}"
        out.append(Factor("Volume profile", "structure", tf, score, why))
    return out


# ---------------------------------------------------------------- derivatives


def funding_zscore(funding: pd.DataFrame | None, current: float | None, window_h: int = 720) -> tuple[float, float]:
    """(z-score of current funding vs trailing window, trailing mean)."""
    if funding is None or len(funding) < 48:
        return float("nan"), float("nan")
    r = funding["rate"].iloc[-window_h:].astype(float)
    cur = float(current) if current is not None else float(r.iloc[-1])
    sd = float(r.std(ddof=0))
    mean = float(r.mean())
    if sd <= 0:
        return 0.0, mean
    return (cur - mean) / sd, mean


def derivatives_factors(funding: pd.DataFrame | None, current_funding: float | None, premium: float | None,
                        oi: pd.DataFrame | None, price_now: float) -> list[Factor]:
    out: list[Factor] = []
    if current_funding is not None:
        z, mean = funding_zscore(funding, current_funding)
        apr = current_funding * 24 * 365 * 100
        why = f"funding {current_funding * 100:.4f}%/h ({current_funding * 8 * 100:.4f}%/8h, {apr:+.1f}% APR)"
        if math.isfinite(z):
            why += f", z {z:+.1f} vs 30d"
            score = _clip(-z / 2, 1.5)
            if abs(z) >= 2:
                why += " — crowded " + ("longs" if z > 0 else "shorts")
        else:
            score = _clip(-apr / 60, 1.0)
        out.append(Factor("Funding", "derivatives", "-", score, why))
    if premium is not None:
        bps = premium * 1e4
        out.append(Factor("Premium", "derivatives", "-", _clip(-bps / 10, 0.75),
                          f"mark premium {bps:+.1f} bps vs oracle"))
    if oi is not None and len(oi) >= 3:
        for label, hours in (("1h", 1), ("4h", 4), ("24h", 24)):
            f = oi_price_quadrant(oi, hours)
            if f is not None:
                out.append(Factor(f"OI vs price {label}", "derivatives", "-", f[0], f[1], scored=False))
    return out


def oi_price_quadrant(oi: pd.DataFrame, hours: int) -> tuple[float, str] | None:
    t_end = oi.index[-1]
    past = oi[oi.index <= t_end - pd.Timedelta(hours=hours)]
    if past.empty:
        return None
    a, b = past.iloc[-1], oi.iloc[-1]
    if a["oi"] <= 0 or a["price"] <= 0:
        return None
    doi = (b["oi"] / a["oi"] - 1) * 100
    dpx = (b["price"] / a["price"] - 1) * 100
    if dpx >= 0 and doi >= 0:
        s, why = 1.0, "price up + OI up (new longs, trend fuel)"
    elif dpx >= 0:
        s, why = 0.25, "price up + OI down (short covering, weaker)"
    elif doi >= 0:
        s, why = -1.0, "price down + OI up (new shorts)"
    else:
        s, why = -0.25, "price down + OI down (long liquidation, weaker)"
    return s, f"{why}: price {dpx:+.2f}%, OI {doi:+.2f}%"


def liquidation_factor(levels: list[Level], price: float) -> Factor | None:
    if not levels:
        return None
    above = [lv for lv in levels if lv.price > price]
    below = [lv for lv in levels if lv.price < price]
    na = sum(lv.strength for lv in above)
    nb = sum(lv.strength for lv in below)
    if na + nb == 0:
        return None
    score = _clip((na - nb) / (na + nb) * 1.0, 1.0)
    return Factor("Liquidation clusters", "derivatives", "-", score,
                  f"liquidity above {na} vs below {nb} (price tends to seek larger clusters)", source="coinglass",
                  scored=False)


# ---------------------------------------------------------------- regime


def btc_regime_factor(btc: dict[str, pd.DataFrame]) -> tuple[Factor | None, int]:
    """Factor plus regime int: +1 strong uptrend, -1 strong downtrend, 0 mixed."""
    signs = []
    for tf in ("1d", "4h"):
        df = btc.get(tf)
        if df is None or len(df) < 60:
            continue
        c, e20, e50 = _last(df, "close"), _last(df, "ema20"), _last(df, "ema50")
        if not (math.isfinite(e20) and math.isfinite(e50)):
            continue
        if c > e20 > e50:
            signs.append(1)
        elif c < e20 < e50:
            signs.append(-1)
        else:
            signs.append(0)
    if not signs:
        return None, 0
    total = sum(signs)
    regime = 1 if total == len(signs) == 2 else (-1 if total == -len(signs) and len(signs) == 2 else 0)
    words = {1: "strong uptrend", -1: "strong downtrend", 0: "mixed"}
    return Factor("BTC trend", "regime", "-", _clip(total * 0.75), f"BTC 1D/4H {words[regime]}", ), regime


def relative_strength(alt_daily: pd.DataFrame, btc_daily: pd.DataFrame, days: int = 30) -> tuple[float, float]:
    """(alt return minus BTC return over `days`, correlation of daily returns)."""
    a = alt_daily["close"].astype(float)
    b = btc_daily["close"].astype(float)
    j = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
    if len(j) < days + 1:
        return float("nan"), float("nan")
    w = j.iloc[-(days + 1) :]
    rs = (w["a"].iloc[-1] / w["a"].iloc[0]) - (w["b"].iloc[-1] / w["b"].iloc[0])
    rets = w.pct_change().dropna()
    corr = float(rets["a"].corr(rets["b"])) if len(rets) > 5 else float("nan")
    return float(rs), corr


def relative_strength_factor(rs: float, corr: float) -> Factor | None:
    if not math.isfinite(rs):
        return None
    why = f"{rs * 100:+.1f}% vs BTC over 30d"
    if math.isfinite(corr):
        why += f", correlation {corr:.2f}"
    return Factor("Relative strength vs BTC", "regime", "-", _clip(rs / 0.10, 1.5), why)


# ---------------------------------------------------------------- sentiment & context


def fear_greed_factor(fg: pd.DataFrame | None) -> Factor | None:
    if fg is None or fg.empty:
        return None
    v = float(fg["value"].iloc[-1])
    if v <= 20:
        score, why = 1.0, "extreme fear (contrarian bullish)"
    elif v >= 80:
        score, why = -1.0, "extreme greed (contrarian bearish)"
    else:
        score, why = (50 - v) / 60, "neutral zone"
    return Factor("Fear & Greed", "sentiment", "-", _clip(score), f"index {v:.0f}: {why}", source="alternative.me",
                  as_of=pd.Timestamp(fg.index[-1]).to_pydatetime())


def cme_gap_factor(gaps: list[CmeGap]) -> Factor | None:
    open_gaps = [g for g in gaps if not g.filled]
    if not open_gaps:
        return None
    g = min(open_gaps, key=lambda x: abs(x.distance_pct))
    direction = "above" if g.distance_pct > 0 else "below"
    return Factor("CME gap", "sentiment", "-", _clip(0.5 if g.distance_pct > 0 else -0.5),
                  f"unfilled CME gap {direction}: {g.gap_low:,.0f}–{g.gap_high:,.0f} ({g.distance_pct:+.1f}%)",
                  source="yahoo BTC=F", scored=False)


def macro_factor(macro: dict[str, pd.DataFrame] | None) -> Factor | None:
    if not macro:
        return None
    parts, score = [], 0.0
    for key, label, sgn in (("spx", "S&P 500", 1), ("dxy", "DXY", -1)):
        df = macro.get(key)
        if df is None or len(df) < 60:
            continue
        c = df["close"].astype(float)
        e50 = c.ewm(span=50, adjust=False).mean()
        up = c.iloc[-1] > e50.iloc[-1]
        score += sgn * (0.75 if up else -0.75)
        parts.append(f"{label} {'above' if up else 'below'} 50D EMA")
    if not parts:
        return None
    tag = "risk-on" if score > 0 else ("risk-off" if score < 0 else "neutral")
    return Factor("Macro regime", "macro", "-", _clip(score), f"{tag}: " + ", ".join(parts), source="yahoo")


def upcoming_events(events: list[EventInfo], now: datetime, hours: float) -> list[EventInfo]:
    lo = now - pd.Timedelta(minutes=30)
    hi = now + pd.Timedelta(hours=hours)
    return [e for e in events if lo <= e.at <= hi]


def crowding(direction_sign: int, funding_z: float, price: float, ema20: float, atrv: float) -> str | None:
    if not (math.isfinite(funding_z) and math.isfinite(ema20) and atrv > 0):
        return None
    ext = (price - ema20) / atrv
    if direction_sign > 0 and funding_z >= 2 and ext >= 2:
        return f"longs crowded: funding z {funding_z:+.1f} and price {ext:.1f} ATR above EMA20"
    if direction_sign < 0 and funding_z <= -2 and ext <= -2:
        return f"shorts crowded: funding z {funding_z:+.1f} and price {abs(ext):.1f} ATR below EMA20"
    return None


def np_finite(x: float) -> bool:
    return bool(np.isfinite(x))
