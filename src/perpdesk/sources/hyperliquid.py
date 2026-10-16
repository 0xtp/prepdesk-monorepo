"""Hyperliquid Info API client (POST /info). Primary data source.

Verified live (scripts/verify_sources.py): `funding` is the hourly rate; `meta` carries
`universe[].marginTableId` and `marginTables` as [[id, {marginTiers:[{lowerBound, maxLeverage}]}]];
candleSnapshot returns at most ~5000 most-recent bars per coin/interval; fundingHistory pages by 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from perpdesk.analysis.types import TF_MS
from perpdesk.sources.base import TokenBucket, request_json
from perpdesk.timeutil import index_ms

NAME = "hyperliquid"
_limiter: TokenBucket | None = None


def limiter(per_minute: int = 700) -> TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(per_minute)
    return _limiter


@dataclass
class PerpInfo:
    name: str
    sz_decimals: int
    max_leverage: int
    margin_table_id: int | None
    only_isolated: bool
    is_delisted: bool


@dataclass
class AssetCtx:
    mark_px: float
    oracle_px: float
    mid_px: float | None
    funding: float  # hourly
    open_interest: float  # in coins
    premium: float | None
    prev_day_px: float
    day_ntl_vlm: float
    impact_pxs: tuple[float, float] | None


@dataclass
class PerpUniverse:
    perps: dict[str, PerpInfo]
    ctxs: dict[str, AssetCtx]
    margin_tables: dict[int, list[tuple[float, int]]]

    def tiers(self, coin: str) -> list[tuple[float, int]]:
        p = self.perps.get(coin)
        if p is None or p.margin_table_id is None:
            return []
        return self.margin_tables.get(p.margin_table_id, [])


class Hyperliquid:
    def __init__(self, base_url: str = "https://api.hyperliquid.xyz", per_minute: int = 700) -> None:
        self.url = base_url.rstrip("/") + "/info"
        self.limiter = limiter(per_minute)

    async def _post(self, body: dict[str, Any], weight: float = 20) -> Any:
        return await request_json(NAME, "POST", self.url, json=body, limiter=self.limiter, weight=weight)

    async def universe(self) -> PerpUniverse:
        meta, ctxs = await self._post({"type": "metaAndAssetCtxs"})
        tables: dict[int, list[tuple[float, int]]] = {}
        for item in meta.get("marginTables", []):
            tid, spec = item[0], item[1]
            tables[int(tid)] = [(float(t["lowerBound"]), int(t["maxLeverage"])) for t in spec.get("marginTiers", [])]
        perps: dict[str, PerpInfo] = {}
        cmap: dict[str, AssetCtx] = {}
        for u, c in zip(meta["universe"], ctxs, strict=False):
            name = u["name"]
            perps[name] = PerpInfo(
                name=name, sz_decimals=int(u["szDecimals"]), max_leverage=int(u["maxLeverage"]),
                margin_table_id=int(u["marginTableId"]) if u.get("marginTableId") is not None else None,
                only_isolated=bool(u.get("onlyIsolated", False)), is_delisted=bool(u.get("isDelisted", False)),
            )
            if c is None or c.get("markPx") is None:
                continue
            imp = c.get("impactPxs")
            cmap[name] = AssetCtx(
                mark_px=float(c["markPx"]), oracle_px=float(c["oraclePx"]),
                mid_px=float(c["midPx"]) if c.get("midPx") else None, funding=float(c["funding"]),
                open_interest=float(c["openInterest"]),
                premium=float(c["premium"]) if c.get("premium") is not None else None,
                prev_day_px=float(c["prevDayPx"]), day_ntl_vlm=float(c["dayNtlVlm"]),
                impact_pxs=(float(imp[0]), float(imp[1])) if imp else None,
            )
        return PerpUniverse(perps, cmap, tables)

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """OHLCV with UTC open-time index. Pages forward if a response stops short of end_ms."""
        step = TF_MS[interval]
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        for _ in range(20):
            expected = max(1, (end_ms - cursor) // step)
            data = await self._post(
                {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": cursor,
                                                   "endTime": end_ms}},
                weight=20 + min(expected, 5000) / 60,
            )
            if not data:
                break
            rows.extend(data)
            last_t = int(data[-1]["t"])
            if last_t + step >= end_ms or len(data) < 500:
                break
            cursor = last_t + step
        return candles_frame(rows)

    async def funding_history(self, coin: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        for _ in range(200):
            body: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": cursor}
            if end_ms is not None:
                body["endTime"] = end_ms
            data = await self._post(body)
            if not data:
                break
            rows.extend(data)
            if len(data) < 500:
                break
            cursor = int(data[-1]["time"]) + 1
        return funding_frame(rows)

    async def l2_book(self, coin: str) -> dict[str, list[tuple[float, float]]]:
        d = await self._post({"type": "l2Book", "coin": coin}, weight=2)
        bids = [(float(x["px"]), float(x["sz"])) for x in d["levels"][0]]
        asks = [(float(x["px"]), float(x["sz"])) for x in d["levels"][1]]
        return {"bids": bids, "asks": asks}

    async def spot_universe(self) -> dict[str, dict[str, Any]]:
        """Spot pairs keyed by base token name: {name: {coin, mark_px, day_ntl_vlm, prev_day_px, sz_decimals}}."""
        meta, ctxs = await self._post({"type": "spotMetaAndAssetCtxs"})
        tokens = {t["index"]: t for t in meta["tokens"]}
        ctx_by_coin = {c["coin"]: c for c in ctxs}
        out: dict[str, dict[str, Any]] = {}
        for pair in meta["universe"]:
            base_i, quote_i = pair["tokens"][0], pair["tokens"][1]
            base, quote = tokens.get(base_i), tokens.get(quote_i)
            if base is None or quote is None or quote["name"] not in ("USDC", "USDT0", "USDH"):
                continue
            c = ctx_by_coin.get(pair["name"])
            if not c or c.get("markPx") is None:
                continue
            nm = base["name"].upper()
            prev = out.get(nm)
            vol = float(c.get("dayNtlVlm", 0) or 0)
            if prev and prev["day_ntl_vlm"] >= vol:
                continue
            out[nm] = {"coin": pair["name"], "mark_px": float(c["markPx"]), "day_ntl_vlm": vol,
                       "prev_day_px": float(c.get("prevDayPx") or c["markPx"]), "sz_decimals": int(base["szDecimals"]),
                       "full_name": base.get("fullName")}
        return out

    # ---- read-only account data (public by address; no keys involved) ----

    async def all_mids(self) -> dict[str, float]:
        d = await self._post({"type": "allMids"}, weight=2)
        return {k: float(v) for k, v in d.items()}

    async def clearinghouse_state(self, user: str) -> dict[str, Any]:
        return dict(await self._post({"type": "clearinghouseState", "user": user}, weight=2))

    async def open_orders(self, user: str) -> list[dict[str, Any]]:
        """frontendOpenOrders: includes trigger (TP/SL) orders with triggerPx, orderType, reduceOnly."""
        return list(await self._post({"type": "frontendOpenOrders", "user": user}))

    async def user_fills(self, user: str, start_ms: int, end_ms: int | None = None) -> list[dict[str, Any]]:
        """Fills since start_ms, oldest first. Pages forward (2000 per response, 10000 most recent available)."""
        out: list[dict[str, Any]] = []
        cursor = start_ms
        for _ in range(10):
            body: dict[str, Any] = {"type": "userFillsByTime", "user": user, "startTime": cursor, "aggregateByTime": True}
            if end_ms is not None:
                body["endTime"] = end_ms
            data = await self._post(body)
            if not data:
                break
            data = sorted(data, key=lambda f: (int(f["time"]), int(f.get("tid", 0))))
            out.extend(data)
            if len(data) < 2000:
                break
            cursor = int(data[-1]["time"]) + 1
        return out

    async def user_funding(self, user: str, start_ms: int, end_ms: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = start_ms
        for _ in range(10):
            body: dict[str, Any] = {"type": "userFunding", "user": user, "startTime": cursor}
            if end_ms is not None:
                body["endTime"] = end_ms
            data = await self._post(body)
            if not data:
                break
            out.extend(data)
            if len(data) < 500:
                break
            cursor = int(data[-1]["time"]) + 1
        return out


def candles_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows)
    df = df.drop_duplicates("t").sort_values("t")
    out = pd.DataFrame(
        {
            "open": df["o"].astype(float).to_numpy(),
            "high": df["h"].astype(float).to_numpy(),
            "low": df["l"].astype(float).to_numpy(),
            "close": df["c"].astype(float).to_numpy(),
            "volume": df["v"].astype(float).to_numpy(),
        },
        index=pd.DatetimeIndex(pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True), name="ts"),
    )
    return out


def funding_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["rate", "premium"], index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time")
    return pd.DataFrame(
        {"rate": df["fundingRate"].astype(float).to_numpy(), "premium": df["premium"].astype(float).to_numpy()},
        index=pd.DatetimeIndex(pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True), name="ts"),
    )


def closed_only(df: pd.DataFrame, interval: str, now_ms: int) -> pd.DataFrame:
    """Drop the forming bar: keep bars whose close time (open + interval) is <= now."""
    if df.empty:
        return df
    close_ms = index_ms(df) + TF_MS[interval]
    return df[close_ms <= now_ms]


def impact_bps(book: dict[str, list[tuple[float, float]]], notional_usd: float, side: str) -> float | None:
    """Average slippage in bps vs mid for a market order of `notional_usd` on `side` ('buy'|'sell')."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    levels = asks if side == "buy" else bids
    remaining, cost, qty = notional_usd, 0.0, 0.0
    for px, sz in levels:
        take = min(remaining, px * sz)
        cost += take
        qty += take / px
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9 or qty == 0:
        return None
    avg = cost / qty
    return abs(avg / mid - 1) * 1e4
