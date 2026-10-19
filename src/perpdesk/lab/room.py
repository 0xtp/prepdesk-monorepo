"""Causal support/resistance levels at a past moment, for backtesting the "room to move" check.

Mirrors the levels a live 1h card shows (analysis/engine.py): 1h pivot clusters, 4H pivot clusters (+1 strength),
untested equal highs/lows and the last completed day and week. Only bars that had closed by time t are used."""

from __future__ import annotations

import numpy as np
import pandas as pd

from perpdesk.analysis import structure as st
from perpdesk.analysis.engine import STRUCTURE_WINDOW
from perpdesk.analysis.indicators import atr
from perpdesk.analysis.types import TF_MS, Level
from perpdesk.timeutil import index_ms
from perpdesk.verdict import Lvl, Room, room_check


class LevelBook:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.f: dict[str, pd.DataFrame] = {tf: frames[tf] for tf in ("1h", "4h", "1d", "1w") if tf in frames and len(frames[tf])}
        self.close: dict[str, np.ndarray] = {tf: index_ms(df) + TF_MS[tf] for tf, df in self.f.items()}
        h1 = self.f.get("1h")
        self.atr1h = atr(h1["high"], h1["low"], h1["close"]).to_numpy() if h1 is not None else None

    def _upto(self, tf: str, t_ms: int, n: int) -> pd.DataFrame | None:
        df = self.f.get(tf)
        if df is None:
            return None
        k = int(np.searchsorted(self.close[tf], t_ms, side="right"))
        return df.iloc[max(0, k - n):k] if k > 0 else None

    def levels_at(self, t_ms: int, price: float) -> list[Lvl]:
        h1 = self._upto("1h", t_ms, STRUCTURE_WINDOW)
        if h1 is None or len(h1) < 60 or self.atr1h is None:
            return []
        k = int(np.searchsorted(self.close["1h"], t_ms, side="right")) - 1
        a = float(self.atr1h[k])
        if not np.isfinite(a) or a <= 0:
            return []
        piv = st.find_pivots(h1, 3)
        raw = st.cluster_levels(piv, 0.5 * a, price)
        h4 = self._upto("4h", t_ms, STRUCTURE_WINDOW)
        if h4 is not None and len(h4) >= 30:
            raw += [Level(lv.price, lv.kind, lv.strength + 1, lv.label + " (4H)")
                    for lv in st.cluster_levels(st.find_pivots(h4, 3), 0.5 * a, price)]
        raw += st.equal_extremes(piv, h1, 0.1 * a)
        raw += st.prior_extremes(self._upto("1d", t_ms, 1), self._upto("1w", t_ms, 1))
        return [Lvl(lv.price, lv.kind, lv.label, lv.strength) for lv in raw]

    def room(self, t_ms: int, direction: str, entry: float, sl: float, tp1: float | None) -> Room:
        return room_check(direction, entry, sl, tp1, self.levels_at(t_ms, entry))
