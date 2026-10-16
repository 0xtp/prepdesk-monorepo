"""Lightweight live prices for the browser poller: current price and 24h change only (no analysis).
Hyperliquid perp/spot contexts are fetched once per ~20-30 s per instance; DEX prices once a minute."""

from __future__ import annotations

from typing import Any

from perpdesk.assets.registry import universe
from perpdesk.db.models import Asset
from perpdesk.sources import dexscreener
from perpdesk.sources.base import CACHE
from perpdesk.sources.hyperliquid import Hyperliquid


async def _spot(hl: Hyperliquid) -> dict[str, dict[str, Any]]:
    hit = CACHE.get("hl:spot:prices")
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    d = {v["coin"]: v for v in (await hl.spot_universe()).values()}
    CACHE.set("hl:spot:prices", d, 30)
    return d


async def _dex(a: Asset) -> tuple[float | None, float | None]:
    key = f"dex:price:{a.chain}:{a.pair_address}"
    hit = CACHE.get(key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    pairs = await dexscreener.token_pairs(a.contract or "")
    pairs = [p for p in pairs if p.pair_address == a.pair_address] or pairs
    out = (pairs[0].price_usd, pairs[0].price_change_h24) if pairs else (None, None)
    CACHE.set(key, out, 60)
    return out


async def live_prices(hl: Hyperliquid, assets: list[Asset]) -> dict[int, dict[str, float | None]]:
    out: dict[int, dict[str, float | None]] = {}
    perps = [a for a in assets if a.perp_available and a.hl_coin]
    spots = [a for a in assets if not (a.perp_available and a.hl_coin) and a.hl_spot_coin]
    dexes = [a for a in assets if a not in perps and a not in spots and a.contract]
    if perps:
        try:
            u = await universe(hl)
            for a in perps:
                c = u.ctxs.get(a.hl_coin or "")
                if c:
                    out[a.id] = {"price": c.mark_px,
                                 "change24": (c.mark_px / c.prev_day_px - 1) * 100 if c.prev_day_px else None}
        except Exception:
            pass
    if spots:
        try:
            sp = await _spot(hl)
            for a in spots:
                v = sp.get(a.hl_spot_coin or "")
                if v:
                    out[a.id] = {"price": v["mark_px"], "change24": (v["mark_px"] / v["prev_day_px"] - 1) * 100
                                 if v.get("prev_day_px") else None}
        except Exception:
            pass
    for a in dexes[:10]:
        try:
            px, ch = await _dex(a)
            if px is not None:
                out[a.id] = {"price": px, "change24": ch}
        except Exception:
            pass
    return out
