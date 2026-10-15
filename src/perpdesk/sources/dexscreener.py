"""DexScreener: token lookup by contract address (any chain) and free-text search. No key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from perpdesk.sources.base import request_json

NAME = "dexscreener"
BASE = "https://api.dexscreener.com"


@dataclass
class DexPair:
    chain_id: str
    dex_id: str
    pair_address: str
    base_symbol: str
    base_name: str
    base_address: str
    quote_symbol: str
    price_usd: float | None
    liquidity_usd: float
    volume_h24: float
    price_change_h24: float | None
    fdv: float | None
    market_cap: float | None
    url: str | None
    image: str | None


def _num(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _pair(p: dict[str, Any]) -> DexPair:
    bt, qt = p.get("baseToken") or {}, p.get("quoteToken") or {}
    info = p.get("info") or {}
    return DexPair(
        chain_id=p.get("chainId", ""), dex_id=p.get("dexId", ""), pair_address=p.get("pairAddress", ""),
        base_symbol=(bt.get("symbol") or "").upper(), base_name=bt.get("name") or "", base_address=bt.get("address") or "",
        quote_symbol=(qt.get("symbol") or "").upper(),
        price_usd=float(p["priceUsd"]) if p.get("priceUsd") else None,
        liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
        volume_h24=float((p.get("volume") or {}).get("h24") or 0),
        price_change_h24=_num((p.get("priceChange") or {}).get("h24")),
        fdv=float(p["fdv"]) if p.get("fdv") else None,
        market_cap=float(p["marketCap"]) if p.get("marketCap") else None,
        url=p.get("url"), image=info.get("imageUrl"),
    )


def looks_like_address(q: str) -> bool:
    q = q.strip()
    if q.startswith("0x") and len(q) == 42:
        return True
    # base58 (Solana etc.): 32-44 chars, no 0/O/I/l
    return 32 <= len(q) <= 44 and all(c.isalnum() and c not in "0OIl" for c in q)


async def token_pairs(address: str) -> list[DexPair]:
    d = await request_json(NAME, "GET", f"{BASE}/latest/dex/tokens/{address.strip()}")
    pairs = [_pair(p) for p in (d.get("pairs") or [])]
    # only pairs where the token is the base asset, best liquidity first
    pairs = [p for p in pairs if p.base_address.lower() == address.strip().lower()] or pairs
    return sorted(pairs, key=lambda p: p.liquidity_usd, reverse=True)


async def search(q: str) -> list[DexPair]:
    d = await request_json(NAME, "GET", f"{BASE}/latest/dex/search", params={"q": q})
    return sorted((_pair(p) for p in (d.get("pairs") or [])), key=lambda p: p.liquidity_usd, reverse=True)
