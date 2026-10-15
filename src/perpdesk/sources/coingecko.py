"""CoinGecko (search, metadata, market data) and GeckoTerminal (DEX pool OHLCV for spot-only tokens)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from perpdesk.sources.base import request_json

NAME = "coingecko"
GT_NAME = "geckoterminal"
BASE = "https://api.coingecko.com/api/v3"
GT = "https://api.geckoterminal.com/api/v2"

# DexScreener chainId -> GeckoTerminal network id
GT_NETWORKS = {
    "ethereum": "eth", "solana": "solana", "base": "base", "bsc": "bsc", "arbitrum": "arbitrum",
    "polygon": "polygon_pos", "avalanche": "avax", "optimism": "optimism", "sui": "sui-network",
    "hyperevm": "hyperevm", "blast": "blast", "linea": "linea", "sonic": "sonic", "ton": "ton", "tron": "tron",
}


def _headers(api_key: str) -> dict[str, str]:
    return {"x-cg-demo-api-key": api_key} if api_key else {}


async def search(q: str, api_key: str = "") -> list[dict[str, Any]]:
    d = await request_json(NAME, "GET", f"{BASE}/search", params={"query": q}, headers=_headers(api_key), retries=1)
    return [
        {"id": c["id"], "symbol": (c.get("symbol") or "").upper(), "name": c.get("name"),
         "rank": c.get("market_cap_rank"), "thumb": c.get("thumb") or c.get("large")}
        for c in d.get("coins", [])[:15]
    ]


async def markets(ids: list[str], api_key: str = "") -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    d = await request_json(NAME, "GET", f"{BASE}/coins/markets",
                           params={"vs_currency": "usd", "ids": ",".join(ids[:200])}, headers=_headers(api_key), retries=1)
    return {c["id"]: c for c in d}


async def pool_ohlcv(chain_id: str, pool: str, timeframe: str = "hour", aggregate: int = 1,
                     limit: int = 1000) -> pd.DataFrame:
    """OHLCV for a DEX pool. timeframe: minute|hour|day; aggregate e.g. 15 (min), 4 (hour)."""
    net = GT_NETWORKS.get(chain_id, chain_id)
    d = await request_json(GT_NAME, "GET", f"{GT}/networks/{net}/pools/{pool}/ohlcv/{timeframe}",
                           params={"aggregate": aggregate, "limit": limit, "currency": "usd"},
                           headers={"accept": "application/json"}, retries=1)
    rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"]).drop_duplicates("t").sort_values("t")
    idx = pd.DatetimeIndex(pd.to_datetime(df["t"].astype("int64"), unit="s", utc=True), name="ts")
    return df[["open", "high", "low", "close", "volume"]].astype(float).set_axis(idx)
