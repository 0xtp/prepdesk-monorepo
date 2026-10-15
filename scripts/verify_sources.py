"""Live proof run: query every data source and print real values side by side.

Usage: python scripts/verify_sources.py [--json out.json]
Exits non-zero if Hyperliquid (the primary source) is unreachable.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx

HL = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "PerpDesk/0.1 (+verify_sources)"}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def check(name: str, fn: Any, results: dict[str, Any]) -> Any:
    t0 = time.perf_counter()
    try:
        data = fn()
        results[name] = {"ok": True, "ms": round((time.perf_counter() - t0) * 1000), "at": now(), "data": data}
    except Exception as e:
        results[name] = {"ok": False, "ms": round((time.perf_counter() - t0) * 1000), "at": now(), "error": repr(e)}
    return results[name]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    c = httpx.Client(timeout=20, headers=UA)
    res: dict[str, Any] = {}

    def hl(body: dict[str, Any]) -> Any:
        r = c.post(HL, json=body)
        r.raise_for_status()
        return r.json()

    def hl_ctx() -> Any:
        meta, ctxs = hl({"type": "metaAndAssetCtxs"})
        out = {"universe_size": len(meta["universe"]), "meta_keys": sorted(meta.keys()), "assets": {}}
        if "marginTables" in meta:
            out["marginTables_sample"] = meta["marginTables"][:3]
        for i, u in enumerate(meta["universe"]):
            if u["name"] in ("BTC", "ETH", "SOL", "HYPE", "UNI", "PENGU", "kPENGU", "ONDO", "AERO", "AVAX", "ZEC", "LINK", "SUI", "DOGE"):
                out["assets"][u["name"]] = {"universe": u, "ctx": ctxs[i]}
        return out

    def hl_candles() -> Any:
        end = int(time.time() * 1000)
        out = {}
        for tf in ("1m", "1h", "1d", "1w"):
            d = hl({"type": "candleSnapshot", "req": {"coin": "SOL", "interval": tf, "startTime": 0, "endTime": end}})
            out[tf] = {"count": len(d), "first_t": d[0]["t"] if d else None, "last": d[-1] if d else None,
                       "span_days": round((d[-1]["t"] - d[0]["t"]) / 86_400_000, 1) if d else 0,
                       "last_open_utc": datetime.fromtimestamp(d[-1]["t"] / 1000, UTC).strftime("%a %Y-%m-%d %H:%M")
                       if d else None}
        return out

    def hl_funding() -> Any:
        d = hl({"type": "fundingHistory", "coin": "SOL", "startTime": 0})
        return {"rows": len(d), "first": d[0] if d else None, "last": d[-1] if d else None}

    def hl_book() -> Any:
        d = hl({"type": "l2Book", "coin": "SOL"})
        return {"bid": d["levels"][0][:2], "ask": d["levels"][1][:2]}

    def hl_spot() -> Any:
        meta, ctxs = hl({"type": "spotMetaAndAssetCtxs"})
        return {"tokens": len(meta["tokens"]), "pairs": len(meta["universe"]), "ctx0": ctxs[0] if ctxs else None}

    def dexs() -> Any:
        r = c.get("https://api.dexscreener.com/latest/dex/search", params={"q": "AERO"})
        r.raise_for_status()
        p = r.json()["pairs"][0]
        return {k: p.get(k) for k in ("chainId", "dexId", "pairAddress", "priceUsd", "liquidity", "volume", "baseToken")}

    def gecko() -> Any:
        r = c.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "bitcoin,solana", "vs_currencies": "usd"})
        r.raise_for_status()
        return r.json()

    def fng() -> Any:
        r = c.get("https://api.alternative.me/fng/", params={"limit": 2, "format": "json"})
        r.raise_for_status()
        return r.json()["data"]

    def yahoo(sym: str) -> Any:
        r = c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": "1mo", "interval": "1d"})
        r.raise_for_status()
        q = r.json()["chart"]["result"][0]
        return {"bars": len(q["timestamp"]), "last_close": q["indicators"]["quote"][0]["close"][-1]}

    def calendar() -> Any:
        r = c.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        r.raise_for_status()
        ev = [e for e in r.json() if e.get("impact") == "High" and e.get("country") == "USD"]
        return {"high_usd_events": len(ev), "sample": ev[:3]}

    def binance_vision() -> Any:
        r = c.get("https://data.binance.vision/?prefix=data/futures/um/monthly/klines/SOLUSDT/1h/")
        return {"status": r.status_code}

    def binance_api() -> Any:
        r = c.get("https://fapi.binance.com/fapi/v1/klines", params={"symbol": "SOLUSDT", "interval": "1h", "limit": 2})
        return {"status": r.status_code, "body": r.text[:200]}

    for name, fn in [("hl_metaAndAssetCtxs", hl_ctx), ("hl_candles", hl_candles), ("hl_funding", hl_funding),
                     ("hl_l2Book", hl_book), ("hl_spot", hl_spot), ("dexscreener", dexs), ("coingecko", gecko),
                     ("fear_greed", fng), ("yahoo_BTC=F", lambda: yahoo("BTC=F")), ("yahoo_DXY", lambda: yahoo("DX-Y.NYB")),
                     ("yahoo_SPX", lambda: yahoo("^GSPC")), ("ff_calendar", calendar),
                     ("binance_vision", binance_vision), ("binance_fapi", binance_api)]:
        r = check(name, fn, res)
        print(f"[{'OK ' if r['ok'] else 'ERR'}] {name:22s} {r['ms']:>5} ms  {r['at']}" + ("" if r["ok"] else f"  {r['error']}"))

    ctx = res["hl_metaAndAssetCtxs"]
    if ctx["ok"]:
        print("\nHyperliquid live (compare with app.hyperliquid.xyz):")
        print(f"{'coin':6s} {'mark':>12s} {'oracle':>12s} {'funding/h':>11s} {'8h %':>8s} {'APR %':>8s} {'OI':>14s} {'maxLev':>6s} {'szDec':>5s}")
        for name, a in ctx["data"]["assets"].items():
            x, u = a["ctx"], a["universe"]
            f = float(x["funding"])
            print(f"{name:6s} {x['markPx']:>12s} {x['oraclePx']:>12s} {f:>11.8f} {f*8*100:>8.4f} {f*24*365*100:>8.2f} "
                  f"{x['openInterest']:>14s} {u.get('maxLeverage')!s:>6s} {u.get('szDecimals')!s:>5s}")
    print("\nDETAIL " + json.dumps(res, default=str)[:20000])
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(res, fh, indent=2, default=str)
    return 0 if ctx["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
