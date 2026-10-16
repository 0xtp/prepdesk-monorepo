"""Sync the watched address: fills + funding into the DB, live positions/orders into KV, round-trip
trades rebuilt and matched to the app's plans. Read-only Info API calls only."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.db.models import PlanRow, WalletFill, WalletFunding, WalletTrade
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import kv_get, kv_get_with_time, kv_set, utcnow
from perpdesk.wallet.advice import Position, parse_positions
from perpdesk.wallet.trades import TradeAgg, classify_exit, r_multiple, rebuild

BACKFILL_MS = 180 * 86_400_000
MATCH_BEFORE_MS = 12 * 3_600_000
MATCH_AFTER_MS = 10 * 60_000


@dataclass
class SyncResult:
    address: str
    new_fills: int = 0
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    error: str | None = None
    skipped: bool = False


def state_key(address: str) -> str:
    return f"wallet:state:{address}"


def load_state(s: Session, address: str) -> tuple[dict[str, Any] | None, Any]:
    hit = kv_get_with_time(s, state_key(address))
    return (hit[0], hit[1]) if hit else (None, None)


def positions_from_state(state: dict[str, Any] | None) -> list[Position]:
    if not state:
        return []
    return parse_positions(state.get("clearinghouse") or {}, state.get("orders") or [])


def _fill_dict(f: WalletFill) -> dict[str, Any]:
    return {"coin": f.coin, "px": f.px, "sz": f.sz, "side": f.side, "time": f.ts, "tid": f.tid,
            "startPosition": f.start_position, "closedPnl": f.closed_pnl, "fee": f.fee, "builderFee": f.builder_fee,
            "liquidation": f.liquidation}


def match_plan(s: Session, coin: str, opened_ms: int) -> PlanRow | None:
    """The plan the user most plausibly acted on: the latest plan for the coin created shortly before the open."""
    return s.execute(select(PlanRow).where(PlanRow.coin == coin, PlanRow.created_ms <= opened_ms + MATCH_AFTER_MS,
                                           PlanRow.created_ms >= opened_ms - MATCH_BEFORE_MS)
                     .order_by(PlanRow.created_ms.desc())).scalars().first()


def _store_fills(s: Session, address: str, fills: list[dict[str, Any]]) -> int:
    have = set(s.execute(select(WalletFill.tid).where(WalletFill.address == address,
                                                      WalletFill.tid.in_([int(f["tid"]) for f in fills]))).scalars())
    n = 0
    for f in fills:
        tid = int(f["tid"])
        if tid in have:
            continue
        have.add(tid)
        s.add(WalletFill(address=address, tid=tid, ts=int(f["time"]), coin=str(f["coin"]), px=float(f["px"]),
                         sz=float(f["sz"]), side=str(f.get("side", "")), dir=str(f.get("dir", ""))[:24],
                         start_position=float(f.get("startPosition") or 0), closed_pnl=float(f.get("closedPnl") or 0),
                         fee=float(f.get("fee") or 0), builder_fee=float(f.get("builderFee") or 0),
                         oid=int(f.get("oid") or 0), crossed=bool(f.get("crossed")),
                         liquidation=bool(f.get("liquidation"))))
        n += 1
    return n


def _store_funding(s: Session, address: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    lo = min(int(ev["time"]) for ev in rows)
    have = {(c, ts) for c, ts in s.execute(select(WalletFunding.coin, WalletFunding.ts)
                                          .where(WalletFunding.address == address, WalletFunding.ts >= lo))}
    for ev in rows:
        d = ev.get("delta") or {}
        if d.get("type") not in (None, "funding") or not d.get("coin"):
            continue
        key = (str(d["coin"]), int(ev["time"]))
        if key in have:
            continue
        have.add(key)
        s.add(WalletFunding(address=address, coin=key[0], ts=key[1], usdc=float(d.get("usdc") or 0),
                            szi=float(d.get("szi") or 0), rate=float(d.get("fundingRate") or 0)))


def _apply(row: WalletTrade, t: TradeAgg, pos: Position | None, s: Session) -> None:
    row.coin, row.direction, row.partial, row.opened_ms = t.coin, t.direction, t.partial, t.opened_ms
    row.closed_ms, row.status = t.closed_ms, "open" if t.is_open else "closed"
    row.entry_px = pos.entry if (pos and t.is_open and pos.entry) else t.entry_px
    row.exit_px = t.exit_px
    row.size = t.max_size
    row.notional = (row.entry_px or 0.0) * t.max_size
    row.realized_pnl, row.fees, row.builder_fees, row.funding = t.realized_pnl, t.fees, t.builder_fees, t.funding
    row.net_pnl, row.n_fills = t.net_pnl, t.n_fills
    if pos is not None:
        row.leverage, row.liq_px = pos.leverage, pos.liq
        if pos.stop is not None:
            row.sl_px = pos.stop
            if row.sl_first is None:
                row.sl_first = pos.stop
        if pos.target is not None:
            row.tp_px = pos.target
    if row.plan_id is None:
        p = match_plan(s, t.coin, t.opened_ms)
        if p is not None:
            row.plan_id, row.plan_direction, row.plan_grade = p.id, p.direction, p.grade
            row.plan_confidence, row.plan_setup = p.confidence, p.setup
            row.followed = p.direction == t.direction
    stop = row.sl_first
    if stop is None and row.plan_id is not None and row.followed:
        plan = s.get(PlanRow, row.plan_id)
        stop = plan.sl if plan else None
    if not t.is_open:
        row.exit_reason = classify_exit(t.direction, row.entry_px, row.exit_px, row.sl_px, row.tp_px, t.liquidated)
        row.r_multiple = r_multiple(t.direction, row.entry_px, stop, t.max_size, t.net_pnl)
    row.updated_at = utcnow()


async def sync_wallet(s: Session, hl: Hyperliquid, settings: Settings, force: bool = False,
                      min_interval_s: float = 30.0) -> SyncResult:
    address = settings.watch_wallet
    res = SyncResult(address)
    if not address:
        res.skipped = True
        return res
    last = kv_get(s, f"wallet:synced:{address}")
    if not force and last and time.time() - float(last) < min_interval_s:
        res.skipped = True
        return res
    now = int(time.time() * 1000)
    try:
        last_fill = s.execute(select(func.max(WalletFill.ts)).where(WalletFill.address == address)).scalar()
        start = (int(last_fill) + 1) if last_fill else now - BACKFILL_MS
        fills = await hl.user_fills(address, start)
        res.new_fills = _store_fills(s, address, fills)
        last_fund = s.execute(select(func.max(WalletFunding.ts)).where(WalletFunding.address == address)).scalar()
        _store_funding(s, address, await hl.user_funding(address, (int(last_fund) + 1) if last_fund else now - BACKFILL_MS))
        state = await hl.clearinghouse_state(address)
        orders = await hl.open_orders(address)
        kv_set(s, state_key(address), {"clearinghouse": state, "orders": orders, "ts": now})
        s.flush()
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res
    positions = {p.coin: p for p in positions_from_state({"clearinghouse": state, "orders": orders})}
    all_fills = [_fill_dict(f) for f in s.execute(select(WalletFill).where(WalletFill.address == address)).scalars()]
    funding = [{"time": r.ts, "delta": {"coin": r.coin, "usdc": r.usdc}}
               for r in s.execute(select(WalletFunding).where(WalletFunding.address == address)).scalars()]
    existing = {r.open_tid: r for r in s.execute(select(WalletTrade).where(WalletTrade.address == address)).scalars()}
    for t in rebuild(all_fills, funding):
        row = existing.get(t.open_tid)
        was_open = row.status == "open" if row else None
        if row is None:
            row = WalletTrade(address=address, open_tid=t.open_tid, notes="", size=0.0, notional=0.0,
                              realized_pnl=0.0, fees=0.0, builder_fees=0.0, funding=0.0, net_pnl=0.0, n_fills=0,
                              coin=t.coin, direction=t.direction, status="open", opened_ms=t.opened_ms,
                              partial=t.partial, updated_at=utcnow())
            s.add(row)
            existing[t.open_tid] = row
        _apply(row, t, positions.get(t.coin) if t.is_open else None, s)
        label = f"{t.coin} {t.direction}"
        if was_open is None and t.is_open:
            res.opened.append(label)
        if (was_open or (was_open is None and last_fill)) and not t.is_open:
            res.closed.append(f"{label} {row.net_pnl:+.2f} USD")
    kv_set(s, f"wallet:synced:{address}", time.time())
    return res
