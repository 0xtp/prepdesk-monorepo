"""Hyperliquid exchange client for live auto-trading.

Orders are signed with the API (agent) wallet from HL_AGENT_PRIVATE_KEY: it can place and cancel orders but cannot
withdraw or transfer. When HL_ACCOUNT_ADDRESS is a sub-account the orders carry it as `vaultAddress` (that is how an
API wallet of the master account trades a sub-account). Signing uses the official hyperliquid-python-sdk helpers;
the HTTP calls go through the app's own client. The key is never logged or shown; only the wallet's public address
is (so you can check it matches the one you authorized)."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from perpdesk.config import Settings
from perpdesk.plan import floor_size
from perpdesk.sources.base import request_json
from perpdesk.sources.hyperliquid import Hyperliquid

MAINNET = "https://api.hyperliquid.xyz"


class LiveConfigError(Exception):
    """Missing or wrong keys / addresses: nothing can be placed."""


class ExchangeError(Exception):
    """The exchange refused an action (the text is Hyperliquid's own message)."""


def ceil_size(qty: float, sz_decimals: int) -> float:
    q = Decimal(1).scaleb(-sz_decimals)
    return float(Decimal(repr(max(qty, 0.0))).quantize(q, rounding=ROUND_CEILING))


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid perp prices: at most 5 significant figures and at most 6 - szDecimals decimals (whole numbers are
    always fine). Same rule as the SDK."""
    if px >= 100_000:
        return float(round(px))
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


def cloid(*parts: Any) -> str:
    """Deterministic 16-byte client order id, so a retried run never places the same order twice."""
    return "0x" + hashlib.sha256(":".join(str(p) for p in ("perpdesk-live", *parts)).encode()).hexdigest()[:32]


@dataclass
class Order:
    coin: str
    is_buy: bool
    sz: float
    px: float  # limit price (for a market trigger: the worst fill price accepted)
    cloid: str
    reduce_only: bool = False
    tif: str = "Gtc"  # Gtc | Ioc | Alo (limit orders)
    trigger_px: float | None = None  # set = trigger order (stop or take-profit)
    tpsl: str = "sl"  # sl | tp (trigger orders)
    trigger_market: bool = True


@dataclass
class Account:
    value: float
    withdrawable: float
    positions: dict[str, dict[str, float]]  # coin -> {"szi": signed size, "entry": entry price}


def agent_address(key: str) -> str:
    from eth_account import Account as EthAccount

    try:
        return str(EthAccount.from_key(key).address).lower()
    except Exception as e:
        raise LiveConfigError("HL_AGENT_PRIVATE_KEY is not a valid private key") from e


class HLExchange:
    """Implements the small interface live/trader.py needs (tests use a fake with the same methods)."""

    def __init__(self, settings: Settings, hl: Hyperliquid) -> None:
        from eth_account import Account as EthAccount

        if not settings.hl_agent_private_key or not settings.hl_account_address:
            raise LiveConfigError("set HL_AGENT_PRIVATE_KEY (API wallet key) and HL_ACCOUNT_ADDRESS (the account it "
                                  "trades) in Vercel → Settings → Environment Variables")
        try:
            self._wallet = EthAccount.from_key(settings.hl_agent_private_key)
        except Exception as e:
            raise LiveConfigError("HL_AGENT_PRIVATE_KEY is not a valid private key") from e
        self.agent = str(self._wallet.address).lower()
        self.account = settings.hl_account_address.lower()
        if self.agent == self.account:
            raise LiveConfigError("HL_ACCOUNT_ADDRESS is the API wallet itself; it must be your (sub-)account address")
        self.base = settings.hyperliquid_api_url.rstrip("/")
        self.mainnet = self.base == MAINNET
        self.hl = hl
        self.vault: str | None = None
        self._ready = False
        self._nonce = 0
        self._meta: dict[str, tuple[int, int, int]] = {}

    # ---------------------------------------------------------------- setup and checks

    async def _info(self, body: dict[str, Any]) -> Any:
        return await request_json("hyperliquid", "POST", self.base + "/info", json=body, retries=1)

    async def prepare(self) -> dict[str, Any]:
        """Check that the API wallet is authorized for this account; decide whether it is a sub-account (vault)."""
        if self._ready:
            return {"agent": self.agent, "vault": bool(self.vault)}
        role = await self._info({"type": "userRole", "user": self.agent})
        if (role or {}).get("role") != "agent":
            raise LiveConfigError(f"the API wallet {self.agent} is not authorized on Hyperliquid (role: "
                                  f"{(role or {}).get('role', '?')}). Create it under More → API and authorize it.")
        master = str(((role or {}).get("data") or {}).get("user", "")).lower()
        if master == self.account:
            self.vault = None
        else:
            subs = await self._info({"type": "subAccounts", "user": master}) or []
            if not any(str(x.get("subAccountUser", "")).lower() == self.account for x in subs):
                raise LiveConfigError("HL_ACCOUNT_ADDRESS is neither the account that authorized the API wallet nor "
                                      "one of its sub-accounts")
            self.vault = self.account
        self._ready = True
        return {"agent": self.agent, "vault": bool(self.vault)}

    async def asset(self, coin: str) -> tuple[int, int, int]:
        """(asset index, size decimals, max leverage)."""
        if not self._meta:
            from perpdesk.assets.registry import universe

            u = await universe(self.hl)
            self._meta = {name: (i, p.sz_decimals, p.max_leverage) for i, (name, p) in enumerate(u.perps.items())}
        if coin not in self._meta:
            raise ExchangeError(f"{coin} is not a Hyperliquid perp")
        return self._meta[coin]

    # ---------------------------------------------------------------- reads

    async def account_state(self) -> Account:
        st = await self.hl.clearinghouse_state(self.account)
        pos = {}
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            szi = float(p.get("szi", 0) or 0)
            if szi:
                pos[p["coin"]] = {"szi": szi, "entry": float(p.get("entryPx") or 0)}
        ms = st.get("marginSummary", {})
        return Account(float(ms.get("accountValue", 0) or 0), float(st.get("withdrawable", 0) or 0), pos)

    async def open_orders(self) -> list[dict[str, Any]]:
        return await self.hl.open_orders(self.account)

    async def fills(self, start_ms: int) -> list[dict[str, Any]]:
        return await self.hl.user_fills(self.account, start_ms)

    async def mids(self) -> dict[str, float]:
        return await self.hl.all_mids()

    # ---------------------------------------------------------------- writes

    def _next_nonce(self) -> int:
        n = max(int(time.time() * 1000), self._nonce + 1)
        self._nonce = n
        return n

    async def _action(self, action: dict[str, Any]) -> Any:
        from hyperliquid.utils.signing import sign_l1_action

        await self.prepare()
        nonce = self._next_nonce()
        sig = sign_l1_action(self._wallet, action, self.vault, nonce, None, self.mainnet)
        body = {"action": action, "nonce": nonce, "signature": sig, "vaultAddress": self.vault}
        # retries=0: an order is not retried blindly; the next run reconciles against the exchange instead
        r = await request_json("hyperliquid-exchange", "POST", self.base + "/exchange", json=body, retries=0)
        if not isinstance(r, dict) or r.get("status") != "ok":
            raise ExchangeError(str((r or {}).get("response", r))[:300] if isinstance(r, dict) else str(r)[:300])
        return r.get("response")

    async def _wire(self, o: Order) -> dict[str, Any]:
        from hyperliquid.utils.signing import order_request_to_order_wire
        from hyperliquid.utils.types import Cloid

        idx, szd, _ = await self.asset(o.coin)
        sz = floor_size(o.sz, szd)
        if sz <= 0:
            raise ExchangeError(f"{o.coin}: size {o.sz} rounds to zero")
        if o.trigger_px is not None:
            otype: Any = {"trigger": {"isMarket": o.trigger_market, "triggerPx": round_px(o.trigger_px, szd),
                                      "tpsl": o.tpsl}}
        else:
            otype = {"limit": {"tif": o.tif}}
        req: Any = {"coin": o.coin, "is_buy": o.is_buy, "sz": sz, "limit_px": round_px(o.px, szd),
                    "order_type": otype, "reduce_only": o.reduce_only, "cloid": Cloid.from_str(o.cloid)}
        return dict(order_request_to_order_wire(req, idx))

    async def place(self, orders: list[Order], grouping: str = "na") -> list[Any]:
        """One status per order: {"resting": {...}}, {"filled": {"totalSz", "avgPx"}}, {"error": "..."} or a word
        such as "waitingForFill" (a stop attached to an entry that has not filled yet)."""
        from hyperliquid.utils.signing import order_wires_to_order_action

        wires = [await self._wire(o) for o in orders]
        resp = await self._action(order_wires_to_order_action(wires, None, grouping))
        return list(((resp or {}).get("data") or {}).get("statuses", []))

    async def cancel(self, items: list[tuple[str, str]]) -> list[Any]:
        """Cancel by (coin, cloid). Already gone orders come back as errors, which is fine."""
        if not items:
            return []
        cancels = [{"asset": (await self.asset(c))[0], "cloid": cl} for c, cl in items]
        resp = await self._action({"type": "cancelByCloid", "cancels": cancels})
        return list(((resp or {}).get("data") or {}).get("statuses", []))

    async def cancel_oids(self, items: list[tuple[str, int]]) -> list[Any]:
        if not items:
            return []
        cancels = [{"a": (await self.asset(c))[0], "o": int(o)} for c, o in items]
        resp = await self._action({"type": "cancel", "cancels": cancels})
        return list(((resp or {}).get("data") or {}).get("statuses", []))

    async def set_leverage(self, coin: str, leverage: int, cross: bool = False) -> None:
        idx, _, _ = await self.asset(coin)
        await self._action({"type": "updateLeverage", "asset": idx, "isCross": cross, "leverage": int(leverage)})
