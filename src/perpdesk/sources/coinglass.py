"""Coinglass (optional, requires COINGLASS_API_KEY). Liquidation levels near price.

Endpoint shapes are taken from Coinglass v4 docs; they can only be verified with a key, so any
parse failure degrades to "unavailable" and the factor is information-only (not scored)."""

from __future__ import annotations

from typing import Any

from perpdesk.analysis.types import Level
from perpdesk.sources.base import request_json

NAME = "coinglass"
BASE = "https://open-api-v4.coinglass.com"


async def liquidation_levels(symbol: str, api_key: str, price: float, max_levels: int = 6) -> list[Level]:
    d: Any = await request_json(NAME, "GET", f"{BASE}/api/futures/liquidation/aggregated-heatmap/model1",
                                params={"symbol": symbol, "range": "3d"}, headers={"CG-API-KEY": api_key}, retries=1)
    data = d.get("data") or {}
    ys = data.get("y_axis") or data.get("y") or []
    cells = data.get("liquidation_leverage_data") or data.get("data") or []
    if not ys or not cells:
        raise ValueError("coinglass: unexpected heatmap shape")
    totals: dict[int, float] = {}
    for cell in cells:
        if len(cell) >= 3:
            totals[int(cell[1])] = totals.get(int(cell[1]), 0.0) + float(cell[2])
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:max_levels]
    if not top:
        return []
    biggest = top[0][1] or 1.0
    out = []
    for yi, amt in top:
        if 0 <= yi < len(ys):
            px = float(ys[yi])
            out.append(Level(px, "liq", max(1, round(3 * amt / biggest)), f"liquidation cluster ${amt / 1e6:,.1f}M"))
    return out
