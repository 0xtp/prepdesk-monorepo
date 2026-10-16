"""Auto-trade sizing (pure): fixed % of equity at risk, leverage only as needed, capped low."""

from __future__ import annotations

import math
from dataclasses import dataclass

from perpdesk.analysis.types import AssetMeta, Direction
from perpdesk.plan import HL_MIN_NOTIONAL_USD, Costs, floor_size, liquidation_price, per_unit_loss, tier_for


@dataclass(frozen=True)
class Sizing:
    ok: bool
    reason: str
    qty: float = 0.0
    leverage: int = 1
    notional: float = 0.0
    margin: float = 0.0
    risk_usd: float = 0.0
    liq: float | None = None


def size_trade(direction: Direction, entry: float, sl: float, c: Costs, equity: float, risk_pct: float,
               max_leverage: int, meta: AssetMeta, liq_buffer: float = 0.0) -> Sizing:
    """qty = (equity × risk%) / loss per coin at the stop (fees, slippage, funding included).
    Leverage = smallest whole number that fits the notional in the equity, capped by max_leverage and the
    exchange tiers; if the cap binds, the size shrinks (risk goes down, never up). Liquidation must sit
    beyond the stop by at least `liq_buffer` (price units)."""
    d = 1 if direction == "LONG" else -1
    if (entry - sl) * d <= 0 or equity <= 0:
        return Sizing(False, "invalid stop or equity")
    budget = equity * risk_pct / 100
    loss = per_unit_loss(entry, sl, c, d)
    notional = budget / loss * entry
    lev_cap = max(1, min(max_leverage, meta.max_leverage, tier_for(notional, meta.margin_tiers, meta.max_leverage)))
    lev = max(1, math.ceil(notional / equity - 1e-9))
    if lev > lev_cap:
        lev = lev_cap
        notional = equity * lev
    qty = floor_size(notional / entry, meta.sz_decimals)
    notional = qty * entry
    if notional < HL_MIN_NOTIONAL_USD:
        return Sizing(False, f"size ${notional:,.2f} below the ${HL_MIN_NOTIONAL_USD:.0f} minimum")
    liq = liquidation_price(entry, qty, notional / lev, direction, meta.margin_tiers, meta.max_leverage)
    if (sl - liq) * d < liq_buffer:
        return Sizing(False, f"liquidation {liq:.6g} too close to the stop {sl:.6g} at {lev}x")
    return Sizing(True, "", qty, lev, notional, notional / lev, qty * loss, liq if liq > entry * 1e-6 else 0.0)
