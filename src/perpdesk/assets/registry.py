"""Resolve a symbol, name or contract address into an asset: Hyperliquid perps first, then Hyperliquid
spot, then CoinGecko (mapped to the most liquid DEX pool), then DexScreener for contract addresses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from perpdesk.db.models import Asset
from perpdesk.sources import coingecko, dexscreener
from perpdesk.sources.base import CACHE
from perpdesk.sources.hyperliquid import Hyperliquid, PerpUniverse

CLASSES = ("Majors", "Large", "Mid", "Memes")
MEME_HINTS = {"DOGE", "PENGU", "PEPE", "kPEPE", "WIF", "BONK", "kBONK", "SHIB", "kSHIB", "FLOKI", "kFLOKI", "POPCAT",
              "FARTCOIN", "TRUMP", "MEW", "BRETT", "SPX", "MOODENG", "PNUT", "GOAT", "TURBO", "NEIRO"}


def hl_icon(coin: str) -> str:
    return f"https://app.hyperliquid.xyz/coins/{coin}.svg"


def class_for(symbol: str, max_leverage: int, group: str | None = None) -> str:
    if group in CLASSES:
        return group
    if symbol in MEME_HINTS:
        return "Memes"
    if max_leverage >= 25:
        return "Majors"
    if max_leverage >= 20:
        return "Large"
    return "Mid"


async def universe(hl: Hyperliquid, ttl: float = 20) -> PerpUniverse:
    hit = CACHE.get("hl:universe")
    if hit is not None:
        return hit[0]  # type: ignore[no-any-return]
    u = await hl.universe()
    from perpdesk.sources.base import utcnow

    CACHE.set("hl:universe", (u, utcnow()), ttl)
    return u


async def spot_universe(hl: Hyperliquid) -> dict[str, dict[str, Any]]:
    hit = CACHE.get("hl:spot")
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    d = await hl.spot_universe()
    CACHE.set("hl:spot", d, 300)
    return d


@dataclass
class SearchResult:
    key: str
    symbol: str
    name: str
    kind: str  # hl-perp | hl-spot | dex | cg
    perp: bool
    detail: str
    icon: str | None = None


def _perp_match(u: PerpUniverse, q: str) -> list[str]:
    qu = q.upper()
    names = [n for n, p in u.perps.items() if not p.is_delisted]
    exact = [n for n in names if n.upper() == qu or n.upper() == "K" + qu]
    starts = sorted(n for n in names if n.upper().startswith(qu) and n not in exact)
    return exact + starts[:8]


async def search(q: str, hl: Hyperliquid, cg_key: str = "") -> list[SearchResult]:
    q = q.strip()
    if not q:
        return []
    out: list[SearchResult] = []
    seen: set[str] = set()
    u = await universe(hl)
    if dexscreener.looks_like_address(q):
        pairs = await dexscreener.token_pairs(q)
        if pairs:
            p = pairs[0]
            key = f"dex:{p.chain_id}:{p.base_address}"
            out.append(SearchResult(key, p.base_symbol, p.base_name, "dex", False,
                                    f"{p.chain_id} · {p.dex_id} · liquidity ${p.liquidity_usd:,.0f} · 24h vol "
                                    f"${p.volume_h24:,.0f}", p.image))
            if p.base_symbol in u.perps:
                out.append(SearchResult(p.base_symbol, p.base_symbol, p.base_name, "hl-perp", True,
                                        "Hyperliquid perp with the same ticker — confirm it is the same token",
                                        hl_icon(p.base_symbol)))
        return out
    for name in _perp_match(u, q):
        ctx = u.ctxs.get(name)
        detail = "Hyperliquid perp"
        if ctx:
            detail += f" · {ctx.mark_px:.6g} · max {u.perps[name].max_leverage}x · 24h vol ${ctx.day_ntl_vlm:,.0f}"
        out.append(SearchResult(name, name, name, "hl-perp", True, detail, hl_icon(name)))
        seen.add(name.upper())
    try:
        spot = await spot_universe(hl)
        for nm, sp in spot.items():
            if nm.startswith(q.upper()) and nm not in seen and len(out) < 12:
                out.append(SearchResult(f"hlspot:{sp['coin']}", nm, sp.get("full_name") or nm, "hl-spot", False,
                                        f"Hyperliquid spot · {sp['mark_px']:.6g} · 24h vol ${sp['day_ntl_vlm']:,.0f}"))
                seen.add(nm)
    except Exception:
        pass
    try:
        coins = await coingecko.search(q, cg_key)
        for c in coins[:8]:
            sym = c["symbol"]
            if sym in seen:
                continue
            perp = sym in u.perps
            key = sym if perp else f"cg:{c['id']}"
            detail = ("Hyperliquid perp" if perp else "CoinGecko · spot via most liquid DEX pool") + (
                f" · rank #{c['rank']}" if c.get("rank") else "")
            out.append(SearchResult(key, sym, c.get("name") or sym, "hl-perp" if perp else "cg", perp, detail,
                                    c.get("thumb")))
            seen.add(sym)
    except Exception:
        pass
    return out


async def create_asset(s: Session, key: str, hl: Hyperliquid, group: str | None = None) -> Asset:
    existing = s.execute(select(Asset).where(Asset.key == key)).scalar_one_or_none()
    if existing:
        return existing
    u = await universe(hl)
    if key in u.perps:
        p = u.perps[key]
        a = Asset(key=key, symbol=key, name=key, hl_coin=key, perp_available=True, sz_decimals=p.sz_decimals,
                  max_leverage=p.max_leverage, margin_tiers=[list(t) for t in u.tiers(key)],
                  only_isolated=p.only_isolated, asset_class=class_for(key, p.max_leverage, group), icon_url=hl_icon(key))
    elif key.startswith("hlspot:"):
        coin = key.split(":", 1)[1]
        spot = await spot_universe(hl)
        nm, sp = next(((n, v) for n, v in spot.items() if v["coin"] == coin), (coin, {"sz_decimals": 2}))
        a = Asset(key=key, symbol=nm, name=sp.get("full_name") or nm, hl_spot_coin=coin, perp_available=False,
                  sz_decimals=int(sp.get("sz_decimals", 2)), max_leverage=1, asset_class=class_for(nm, 1, group))
    elif key.startswith("dex:"):
        _, chain, addr = key.split(":", 2)
        pairs = await dexscreener.token_pairs(addr)
        pairs = [p for p in pairs if p.chain_id == chain] or pairs
        if not pairs:
            raise ValueError(f"no DEX pairs found for {addr}")
        dp = pairs[0]
        a = Asset(key=key, symbol=dp.base_symbol, name=dp.base_name, chain=dp.chain_id, contract=dp.base_address,
                  pair_address=dp.pair_address, perp_available=False, sz_decimals=4, max_leverage=1,
                  asset_class=class_for(dp.base_symbol, 1, group), icon_url=dp.image)
    elif key.startswith("cg:"):
        cg_id = key.split(":", 1)[1]
        coins = await coingecko.search(cg_id)
        sym = next((c["symbol"] for c in coins if c["id"] == cg_id), cg_id.upper())
        pairs = [p for p in await dexscreener.search(sym) if p.base_symbol == sym]
        if not pairs:
            raise ValueError(f"{sym}: no perp and no DEX pool found to analyse")
        cp = pairs[0]
        a = Asset(key=f"dex:{cp.chain_id}:{cp.base_address}", symbol=sym, name=cp.base_name, cg_id=cg_id,
                  chain=cp.chain_id, contract=cp.base_address, pair_address=cp.pair_address, perp_available=False,
                  sz_decimals=4, max_leverage=1, asset_class=class_for(sym, 1, group), icon_url=cp.image)
        dup = s.execute(select(Asset).where(Asset.key == a.key)).scalar_one_or_none()
        if dup:
            return dup
    else:
        raise ValueError(f"unknown asset {key!r}: not a Hyperliquid perp")
    s.add(a)
    s.flush()
    return a


async def refresh_perp_meta(s: Session, hl: Hyperliquid) -> None:
    """Keep size decimals / max leverage / margin tiers current."""
    u = await universe(hl)
    for a in s.execute(select(Asset).where(Asset.hl_coin.is_not(None))).scalars():
        p = u.perps.get(a.hl_coin or "")
        if p is None:
            a.perp_available = False
            continue
        a.sz_decimals, a.max_leverage = p.sz_decimals, p.max_leverage
        a.margin_tiers = [list(t) for t in u.tiers(p.name)]
        a.perp_available = not p.is_delisted


async def gather_limited(coros: list[Any], limit: int = 4) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def run(c: Any) -> Any:
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
