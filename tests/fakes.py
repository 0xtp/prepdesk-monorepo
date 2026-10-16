"""TEST-ONLY fake upstream APIs (respx). Never imported by application code."""
from __future__ import annotations

import json
import time

import httpx
import numpy as np
import respx

from perpdesk.analysis.types import TF_MS

COINS = {"BTC": (82000.0, 5, 40, 56), "ETH": (2450.0, 4, 25, 55), "SOL": (106.0, 2, 20, 54),
         "HYPE": (81.9, 2, 10, 52), "AVAX": (10.0, 2, 10, 52)}


def _price(coin: str, t_ms: int) -> float:
    base = COINS.get(coin, (1.0, 2, 10, 52))[0]
    seed = sum(map(ord, coin))
    x = t_ms / 3_600_000
    return base * (1 + 0.05 * np.sin(x / 50 + seed) + 0.02 * np.sin(x / 7 + seed * 3) + 0.00002 * (x % 1000))


def candles(coin: str, interval: str, start: int, end: int) -> list[dict]:
    step = TF_MS[interval]
    first = max(start - start % step, end - 5000 * step - (end % step))
    out = []
    t = first
    while t + step <= end + step and len(out) < 5000:
        o = _price(coin, t)
        c = _price(coin, t + step)
        h = max(o, c) * 1.002
        lo = min(o, c) * 0.998
        out.append({"t": t, "T": t + step - 1, "s": coin, "i": interval, "o": str(o), "c": str(c), "h": str(h),
                    "l": str(lo), "v": "1000", "n": 10})
        t += step
    return [x for x in out if x["t"] >= start and x["t"] <= end]


WALLET = "0x" + "ab" * 20
_T0 = int(time.time() * 1000) - 3 * 3_600_000
WALLET_FILLS = [
    {"coin": "SOL", "px": "100", "sz": "10", "side": "B", "time": _T0, "tid": 1, "startPosition": "0",
     "dir": "Open Long", "closedPnl": "0", "fee": "0.45", "builderFee": "0.1", "oid": 11, "crossed": True},
    {"coin": "SOL", "px": "110", "sz": "10", "side": "A", "time": _T0 + 3_600_000, "tid": 2, "startPosition": "10",
     "dir": "Close Long", "closedPnl": "100", "fee": "0.5", "builderFee": "0.1", "oid": 12, "crossed": True},
    {"coin": "ETH", "px": "2450", "sz": "1", "side": "A", "time": _T0 + 2 * 3_600_000, "tid": 3, "startPosition": "0",
     "dir": "Open Short", "closedPnl": "0", "fee": "1.1", "oid": 13, "crossed": True},
    {"coin": "@107", "px": "1", "sz": "5", "side": "B", "time": _T0, "tid": 4, "startPosition": "0", "dir": "Buy",
     "closedPnl": "0", "fee": "0", "oid": 14, "crossed": True},
]
WALLET_STATE = {
    "assetPositions": [{"type": "oneWay", "position": {
        "coin": "ETH", "szi": "-1.0", "entryPx": "2450", "positionValue": "2440", "unrealizedPnl": "10",
        "returnOnEquity": "0.04", "liquidationPx": "2600", "marginUsed": "245", "maxLeverage": 25,
        "leverage": {"type": "isolated", "value": 10, "rawUsd": "2695"},
        "cumFunding": {"allTime": "-0.5", "sinceOpen": "-0.5", "sinceChange": "-0.5"}}}],
    "marginSummary": {"accountValue": "1000.0", "totalNtlPos": "2440", "totalRawUsd": "0", "totalMarginUsed": "245"},
    "withdrawable": "755.0", "time": 0,
}
WALLET_ORDERS = [{"coin": "ETH", "side": "B", "isTrigger": True, "triggerPx": "2420", "limitPx": "2420",
                  "orderType": "Take Profit Market", "reduceOnly": True, "isPositionTpsl": True, "sz": "1", "oid": 99,
                  "timestamp": 0, "triggerCondition": "Price below 2420", "origSz": "1"}]


def info_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    typ = body["type"]
    now = int(time.time() * 1000)
    if typ == "metaAndAssetCtxs":
        uni = [{"name": c, "szDecimals": v[1], "maxLeverage": v[2], "marginTableId": v[3]} for c, v in COINS.items()]
        ctxs = [{"funding": "0.0000125", "openInterest": "1000", "prevDayPx": str(_price(c, now - 86_400_000)),
                 "dayNtlVlm": "500000000", "premium": "-0.0001", "oraclePx": str(_price(c, now)),
                 "markPx": str(_price(c, now)), "midPx": str(_price(c, now)), "impactPxs": ["1", "1"]} for c in COINS]
        tables = [[52, {"description": "tiered 10x (2)", "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 10},
                                                                          {"lowerBound": "20000000.0", "maxLeverage": 5}]}]]
        return httpx.Response(200, json=[{"universe": uni, "marginTables": tables, "collateralToken": 0}, ctxs])
    if typ == "candleSnapshot":
        r = body["req"]
        return httpx.Response(200, json=candles(r["coin"], r["interval"], r["startTime"], r["endTime"]))
    if typ == "fundingHistory":
        start = max(body["startTime"], now - 30 * 86_400_000)
        rows = [{"coin": body["coin"], "fundingRate": "0.0000125", "premium": "-0.0001", "time": t}
                for t in range(start - start % 3_600_000, now, 3_600_000)][:500]
        return httpx.Response(200, json=rows)
    if typ == "spotMetaAndAssetCtxs":
        meta = {"tokens": [{"name": "USDC", "index": 0, "szDecimals": 8}, {"name": "PURR", "index": 1, "szDecimals": 0}],
                "universe": [{"name": "PURR/USDC", "tokens": [1, 0], "index": 0}]}
        return httpx.Response(200, json=[meta, [{"coin": "PURR/USDC", "markPx": "0.1", "prevDayPx": "0.1",
                                                 "dayNtlVlm": "100000"}]])
    if typ == "l2Book":
        return httpx.Response(200, json={"levels": [[{"px": "100", "sz": "10", "n": 1}], [{"px": "100.1", "sz": "10", "n": 1}]]})
    if typ == "userFillsByTime":
        return httpx.Response(200, json=[f for f in WALLET_FILLS if f["time"] >= body["startTime"]])
    if typ == "userFunding":
        rows = [{"time": _T0 + 2 * 3_600_000 + 1800_000, "hash": "0x0",
                 "delta": {"type": "funding", "coin": "ETH", "usdc": "0.5", "szi": "-1", "fundingRate": "0.00001"}}]
        return httpx.Response(200, json=[r for r in rows if r["time"] >= body["startTime"]])
    if typ == "clearinghouseState":
        return httpx.Response(200, json=WALLET_STATE)
    if typ == "frontendOpenOrders":
        return httpx.Response(200, json=WALLET_ORDERS)
    if typ == "allMids":
        return httpx.Response(200, json={c: str(_price(c, now)) for c in COINS})
    return httpx.Response(400, json={"error": typ})


def install(router: respx.MockRouter) -> None:
    router.post("https://api.hyperliquid.xyz/info").mock(side_effect=info_handler)
    router.get(url__startswith="https://api.alternative.me/fng/").mock(return_value=httpx.Response(200, json={
        "data": [{"value": "40", "value_classification": "Fear", "timestamp": str(int(time.time()) - 86400 * i)}
                 for i in range(5)]}))
    now = int(time.time())
    days = [now - 86400 * i for i in range(200, 0, -1)]
    chart = {"chart": {"result": [{"timestamp": days, "indicators": {"quote": [{"open": [100 + i * 0.1 for i in range(200)],
             "high": [101 + i * 0.1 for i in range(200)], "low": [99 + i * 0.1 for i in range(200)],
             "close": [100.5 + i * 0.1 for i in range(200)]}]}}]}}
    router.get(url__startswith="https://query1.finance.yahoo.com/").mock(return_value=httpx.Response(200, json=chart))
    router.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json").mock(return_value=httpx.Response(200, json=[
        {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"}]))
    pair = {"chainId": "base", "dexId": "aerodrome", "pairAddress": "0xpool", "priceUsd": "0.5",
            "baseToken": {"address": "0xabc0000000000000000000000000000000000001", "name": "Test Token", "symbol": "TST"},
            "quoteToken": {"symbol": "USDC"}, "liquidity": {"usd": 5_000_000}, "volume": {"h24": 3_000_000},
            "priceChange": {"h24": 2.5}, "info": {"imageUrl": None}}
    router.get(url__startswith="https://api.dexscreener.com/latest/dex/tokens/").mock(
        return_value=httpx.Response(200, json={"pairs": [pair]}))
    router.get(url__startswith="https://api.dexscreener.com/latest/dex/search").mock(
        return_value=httpx.Response(200, json={"pairs": [pair]}))
    router.get(url__startswith="https://api.coingecko.com/api/v3/search").mock(return_value=httpx.Response(200, json={
        "coins": [{"id": "solana", "symbol": "sol", "name": "Solana", "market_cap_rank": 6, "thumb": None}]}))

    def gt(request: httpx.Request) -> httpx.Response:
        tfname = request.url.path.rsplit("/", 1)[1]
        agg = int(request.url.params.get("aggregate", 1))
        step = {"minute": 60, "hour": 3600, "day": 86400}[tfname] * agg
        end = int(time.time()) - int(time.time()) % step
        rows = [[t, 0.5, 0.505, 0.495, 0.5 + 0.01 * np.sin(t / 50000), 1000.0] for t in range(end - step * 999, end, step)]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": rows[::-1]}}})

    router.get(url__startswith="https://api.geckoterminal.com/").mock(side_effect=gt)
