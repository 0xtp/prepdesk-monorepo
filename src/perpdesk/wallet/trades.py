"""Rebuild round-trip trades from Hyperliquid fills (pure functions, no I/O).

A trade starts when a coin's position leaves zero and ends when it returns to zero. A fill that flips
the position ("Long > Short") closes one trade and opens the next; its closed PnL goes to the closing
part and its fee is split by size. Spot fills (coin "@123" or "PURR/USDC") are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EPS = 1e-9


def is_perp_coin(coin: str) -> bool:
    return not coin.startswith("@") and "/" not in coin


@dataclass
class TradeAgg:
    coin: str
    direction: str  # LONG|SHORT
    open_tid: int
    opened_ms: int
    partial: bool = False  # opened before the fetched history began: entry is unknown or incomplete
    closed_ms: int | None = None
    entry_qty: float = 0.0
    entry_notional: float = 0.0
    exit_qty: float = 0.0
    exit_notional: float = 0.0
    max_size: float = 0.0
    realized_pnl: float = 0.0  # Hyperliquid closedPnl (gross of fees)
    fees: float = 0.0  # exchange + builder fees
    builder_fees: float = 0.0
    funding: float = 0.0  # positive = received
    liquidated: bool = False
    n_fills: int = 0
    fill_tids: list[int] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_ms is None

    @property
    def entry_px(self) -> float | None:
        return self.entry_notional / self.entry_qty if self.entry_qty > EPS else None

    @property
    def exit_px(self) -> float | None:
        return self.exit_notional / self.exit_qty if self.exit_qty > EPS else None

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.fees + self.funding


def _f(x: Any) -> float:
    return float(x) if x not in (None, "") else 0.0


def rebuild(fills: list[dict[str, Any]], funding: list[dict[str, Any]] | None = None) -> list[TradeAgg]:
    """Fills in Hyperliquid userFills format (any order). Returns trades oldest first."""
    rows = sorted((f for f in fills if is_perp_coin(str(f.get("coin", "")))),
                  key=lambda f: (int(f["time"]), int(f.get("tid", 0))))
    open_by_coin: dict[str, TradeAgg] = {}
    out: list[TradeAgg] = []
    for f in rows:
        coin = str(f["coin"])
        px, sz, ts, tid = _f(f["px"]), _f(f["sz"]), int(f["time"]), int(f.get("tid", 0))
        start = _f(f.get("startPosition"))
        signed = sz if f.get("side") == "B" else -sz
        end = start + signed
        if abs(end) < EPS * max(1.0, abs(start)):
            end = 0.0
        bfee = _f(f.get("builderFee"))
        fee, pnl = _f(f.get("fee")) + bfee, _f(f.get("closedPnl"))
        cur = open_by_coin.get(coin)
        if cur is None and abs(start) > EPS:
            # position existed before our history: start a partial trade carrying the prior size
            cur = TradeAgg(coin, "LONG" if start > 0 else "SHORT", tid, ts, partial=True, max_size=abs(start))
            open_by_coin[coin] = cur
            out.append(cur)
        same_side = start == 0 or end == 0 or (start > 0) == (end > 0)
        closing = abs(start) if not same_side else max(0.0, abs(start) - abs(end))
        opening = abs(end) if not same_side else max(0.0, abs(end) - abs(start))
        if cur is not None and closing > EPS:
            share = closing / sz if sz > EPS else 1.0
            cur.exit_qty += closing
            cur.exit_notional += closing * px
            cur.realized_pnl += pnl
            cur.fees += fee * share
            cur.builder_fees += bfee * share
            cur.n_fills += 1
            cur.fill_tids.append(tid)
            if f.get("liquidation"):
                cur.liquidated = True
            fee, bfee, pnl = fee * (1 - share), bfee * (1 - share), 0.0
            if end == 0 or not same_side:
                cur.closed_ms = ts
                del open_by_coin[coin]
                cur = None
        if opening > EPS:
            if cur is None:
                cur = TradeAgg(coin, "LONG" if end > 0 else "SHORT", tid, ts)
                open_by_coin[coin] = cur
                out.append(cur)
            cur.entry_qty += opening
            cur.entry_notional += opening * px
            cur.fees += fee
            cur.builder_fees += bfee
            cur.realized_pnl += pnl
            cur.n_fills += 1
            cur.fill_tids.append(tid)
            cur.max_size = max(cur.max_size, abs(end))
    for ev in funding or []:
        d = ev.get("delta") or ev
        coin, ts, usdc = str(d.get("coin", "")), int(ev.get("time", 0)), _f(d.get("usdc"))
        for t in out:
            if t.coin == coin and t.opened_ms <= ts and (t.closed_ms is None or ts <= t.closed_ms):
                t.funding += usdc
                break
    return out


def classify_exit(direction: str, entry: float | None, exit_px: float | None, sl: float | None, tp: float | None,
                  liquidated: bool) -> str:
    """Best-effort label for how a closed trade ended."""
    if liquidated:
        return "liquidated"
    if exit_px is None or entry is None:
        return "closed"
    sign = 1 if direction == "LONG" else -1
    tol = abs(entry) * 0.002
    if sl is not None and sign * (exit_px - sl) <= tol:
        return "stop"
    if tp is not None and sign * (exit_px - tp) >= -tol:
        return "target"
    return "manual"


def r_multiple(direction: str, entry: float | None, stop: float | None, qty: float, net_pnl: float) -> float | None:
    """Net P&L expressed in units of the initial risk (entry→stop distance × size)."""
    if entry is None or stop is None or qty <= EPS:
        return None
    risk = abs(entry - stop) * qty
    if risk <= EPS or (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
        return None
    return net_pnl / risk
