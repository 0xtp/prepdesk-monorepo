"""CME Bitcoin futures weekend gaps.

CME BTC futures close Friday 16:00 CT and reopen Sunday 17:00 CT. A gap is the price range
between Friday's close and the first session's open after the weekend. It is "filled" once
spot/perp price has traded back through the whole gap (checked with Hyperliquid BTC candles).
"""

from __future__ import annotations

import pandas as pd

from perpdesk.analysis.types import CmeGap


def find_gaps(cme_daily: pd.DataFrame, btc_hourly: pd.DataFrame | None, price: float, weeks: int = 8,
              min_gap_pct: float = 0.1) -> list[CmeGap]:
    """cme_daily: index = session date (UTC midnight), columns open/close. Returns most recent first."""
    if cme_daily is None or len(cme_daily) < 3:
        return []
    df = cme_daily.dropna(subset=["open", "close"]).sort_index()
    out: list[CmeGap] = []
    idx = list(df.index)
    for i in range(1, len(idx)):
        prev, cur = pd.Timestamp(idx[i - 1]), pd.Timestamp(idx[i])
        # weekend boundary: previous session on Friday, next on Monday (or later after a holiday)
        if (cur - prev).days < 3 or prev.weekday() != 4:
            continue
        fri_close = float(df.loc[idx[i - 1], "close"])
        mon_open = float(df.loc[idx[i], "open"])
        lo, hi = sorted((fri_close, mon_open))
        if hi <= 0 or (hi - lo) / hi * 100 < min_gap_pct:
            continue
        # Sunday 22:00 UTC is when the CME session opens (17:00 CT, approx.)
        opened = cur - pd.Timedelta(hours=2)
        filled = False
        if btc_hourly is not None and not btc_hourly.empty:
            after = btc_hourly[btc_hourly.index >= opened]
            if not after.empty:
                if mon_open > fri_close:  # gap up: filled when price trades down to Friday close
                    filled = bool((after["low"] <= lo).any())
                else:
                    filled = bool((after["high"] >= hi).any())
        if price > hi:
            dist = (hi / price - 1) * 100
        elif price < lo:
            dist = (lo / price - 1) * 100
        else:
            dist = 0.0
        out.append(CmeGap(lo, hi, opened, filled, dist))
    out.reverse()
    return out[:weeks]
