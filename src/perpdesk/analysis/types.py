"""Shared value types for the analysis engine. Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import pandas as pd

Direction = Literal["LONG", "SHORT"]
Move = Literal["UP", "DOWN", "SIDEWAYS"]
Grade = Literal["A", "B", "C"]
Group = Literal["trend", "momentum", "volatility", "structure", "derivatives", "regime", "sentiment", "macro"]

TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}


def sign(d: Direction) -> int:
    return 1 if d == "LONG" else -1


@dataclass(frozen=True)
class Factor:
    name: str
    group: Group
    tf: str  # timeframe, or "-" for non-timeframe factors
    score: float  # -2 .. +2
    reason: str
    source: str = "hyperliquid"
    as_of: datetime | None = None
    scored: bool = True  # False = shown for information, excluded from the composite score

    def __post_init__(self) -> None:
        if not -2.0 <= self.score <= 2.0:
            raise ValueError(f"factor {self.name} score {self.score} out of range")


@dataclass(frozen=True)
class Level:
    price: float
    kind: str  # support | resistance | pdh | pdl | pwh | pwl | eqh | eql | vwap | poc | vah | val | ema | fvg
    strength: int = 1
    label: str = ""


@dataclass(frozen=True)
class Pivot:
    idx: int
    ts: pd.Timestamp
    price: float
    kind: Literal["high", "low"]


@dataclass(frozen=True)
class RangeInfo:
    high: float
    low: float
    touches_high: int
    touches_low: int

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


@dataclass(frozen=True)
class Flag:
    code: str  # event | volatility | stale | liquidity | liq_buffer | btc_counter | crowded | low_rr | calibration | ...
    reason: str
    reduces_size: bool = True


@dataclass(frozen=True)
class CmeGap:
    gap_low: float
    gap_high: float
    opened: pd.Timestamp
    filled: bool
    distance_pct: float  # signed distance from current price to the nearest gap edge


@dataclass
class EventInfo:
    title: str
    at: datetime
    impact: str
    country: str


@dataclass
class AssetMeta:
    coin: str
    is_perp: bool
    sz_decimals: int = 2
    max_leverage: int = 1
    margin_tiers: list[tuple[float, int]] = field(default_factory=list)  # (lower_bound_notional_usd, max_leverage)
    only_isolated: bool = False
    asset_class: str = "Mid"


@dataclass
class MarketSnapshot:
    """Everything the engine may look at, all truncated at `asof` (no future data)."""

    coin: str
    asof: pd.Timestamp
    price: float
    meta: AssetMeta
    candles: dict[str, pd.DataFrame]  # tf -> closed bars, columns open/high/low/close/volume, UTC index = open time
    btc_candles: dict[str, pd.DataFrame] | None = None  # None when coin is BTC
    funding: pd.DataFrame | None = None  # index ts, columns rate (hourly), premium
    oi: pd.DataFrame | None = None  # index ts, columns oi, price  (our own snapshots)
    current_funding: float | None = None
    current_premium: float | None = None
    open_interest: float | None = None
    day_volume_usd: float | None = None
    liquidity_usd: float | None = None
    fear_greed: pd.DataFrame | None = None  # index ts, column value
    cme_daily: pd.DataFrame | None = None
    macro: dict[str, pd.DataFrame] | None = None  # "dxy" / "spx" daily closes
    events: list[EventInfo] = field(default_factory=list)
    liquidation_levels: list[Level] = field(default_factory=list)
    book_impact_bps: float | None = None
    source_times: dict[str, datetime | None] = field(default_factory=dict)
    stale_sources: list[str] = field(default_factory=list)


@dataclass
class SetupSignal:
    name: str  # trend_pullback | range_reclaim | breakout | funding_fade | composite
    direction: Direction
    entry_type: Literal["limit", "market"]
    params: dict[str, float] = field(default_factory=dict)
    note: str = ""


@dataclass
class Analysis:
    coin: str
    asof: pd.Timestamp
    price: float
    trading_tf: str
    factors: list[Factor]
    group_scores: dict[str, float]
    score: float  # -1 .. +1
    direction: Direction
    confidence: int  # 0 .. 100
    expected_move: Move
    setup: SetupSignal
    atr: float
    atr_pctl: float
    levels: list[Level]
    pivots: list[Pivot]
    range: RangeInfo | None
    flags: list[Flag]
    top_for: list[Factor]
    top_against: list[Factor]
    htf_direction: Direction
    cme_gaps: list[CmeGap] = field(default_factory=list)
    near_zero: bool = False
    notes: list[str] = field(default_factory=list)
