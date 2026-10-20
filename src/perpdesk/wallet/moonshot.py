"""Moonshot bucket: a separate Hyperliquid account (a sub-account or second wallet) holding only money you can
afford to lose. Watch-only, like My trades. The app tracks its value, profit and loss net of deposits and
withdrawals, and holds you to the rules you agreed: never top it up after a loss, take the stake out when it
doubles, and stop when it is gone. Nothing here places or changes orders."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from perpdesk.config import Settings
from perpdesk.sources.hyperliquid import Hyperliquid
from perpdesk.store import kv_get, kv_set
from perpdesk.wallet.advice import parse_positions

SHARE_WARN = 0.10  # warn when the bucket is more than 10% of everything you track
RULES = [
    "Only money you could lose completely without it hurting you.",
    "Never top it up after a loss. A top-up is how a small bet becomes a big loss.",
    "When it doubles, take your original stake out; play only with the profit.",
    "When it's gone, it's gone: stop until next month, don't refill.",
    "Always set a stop. The app's TAKE rules and limits don't apply here, so the stop is your only protection.",
]


def key(address: str) -> str:
    return f"moonshot:{address.lower()}"


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def net_flow(update: dict[str, Any], address: str) -> float:
    """USD moved into (+) or out of (−) this account by one ledger update. Trading P&L and moves between the
    account's own spot and perp balances are not flows."""
    d = update.get("delta") or {}
    kind = d.get("type")
    me = address.lower()
    if kind == "deposit":
        return _f(d.get("usdc"))
    if kind == "withdraw":
        return -(_f(d.get("usdc")) + _f(d.get("fee")))
    if kind in ("internalTransfer", "subAccountTransfer", "send"):
        amt = _f(d.get("usdc") or d.get("amount") or d.get("usdcValue"))
        if str(d.get("destination", "")).lower() == me:
            return amt
        if str(d.get("user", "")).lower() == me:
            return -(amt + _f(d.get("fee")))
        return 0.0
    if kind == "spotTransfer":
        amt = _f(d.get("usdcValue"))
        return amt if str(d.get("destination", "")).lower() == me else -amt
    if kind == "vaultDeposit":
        return -_f(d.get("usdc"))
    if kind in ("vaultWithdraw", "vaultDistribution"):
        return _f(d.get("netWithdrawnUsd") or d.get("usdc"))
    return 0.0


def spot_usdc(spot: dict[str, Any]) -> float:
    return sum(_f(b.get("total")) for b in spot.get("balances") or [] if b.get("coin") == "USDC")


async def sync(s: Session, hl: Hyperliquid, settings: Settings, now_ms: int) -> dict[str, Any] | None:
    addr = settings.moonshot_wallet
    if not addr:
        return None
    if settings.watch_wallet and addr == settings.watch_wallet:
        return {"error": "The moonshot account must be separate from your main wallet (use a Hyperliquid "
                         "sub-account or a second wallet)."}
    prev = kv_get(s, key(addr)) or {}
    perp = await hl.clearinghouse_state(addr)
    spot = await hl.spot_state(addr)
    perp_value = _f((perp.get("marginSummary") or {}).get("accountValue"))
    value = perp_value + spot_usdc(spot)
    start_ms = int(prev.get("start_ms") or now_ms)
    since = int(prev.get("ledger_ms") or start_ms)
    ups = await hl.ledger_updates(addr, since) if prev else []
    seen = set(prev.get("seen") or [])
    net_in = float(prev.get("net_in") or 0.0)
    sv = prev.get("start_value")
    start_value = _f(sv) if sv is not None else value
    topups = list(prev.get("topups") or [])
    last_pnl = float(prev.get("pnl") or 0.0)
    for u in ups:
        h = str(u.get("hash") or u.get("time"))
        if h in seen:
            continue
        seen.add(h)
        flow = net_flow(u, addr)
        if flow > 0 and last_pnl < 0:
            topups.append({"ms": int(u.get("time", now_ms)), "usd": flow, "pnl_before": last_pnl})
        net_in += flow
    pnl = value - start_value - net_in
    budget = settings.moonshot_budget_usd or (start_value + net_in) or None
    positions = [{"coin": p.coin, "direction": p.direction, "size_usd": p.as_dict()["notional"],
                  "leverage": p.leverage, "upnl": p.upnl, "liq": p.liq, "has_stop": p.stop is not None}
                 for p in parse_positions(perp, await hl.open_orders(addr))]
    hist = list(prev.get("history") or [])
    if not hist or now_ms - int(hist[-1][0]) >= 6 * 3_600_000:
        hist.append([now_ms, round(value, 2)])
    st = {"start_ms": start_ms, "start_value": start_value, "net_in": net_in, "value": value, "pnl": pnl,
          "budget": budget, "positions": positions, "topups": topups[-20:], "history": hist[-400:],
          "peak": max(float(prev.get("peak") or value), value), "ledger_ms": now_ms - 60_000,
          "seen": sorted(seen)[-500:], "alerted": prev.get("alerted") or {}, "updated_ms": now_ms}
    kv_set(s, key(addr), st)
    return st


def main_value(s: Session, settings: Settings) -> float | None:
    """Value of the main watched wallet (from its last sync), to check the bucket stays small."""
    from perpdesk.wallet.sync import load_state

    if not settings.watch_wallet:
        return None
    state, _ = load_state(s, settings.watch_wallet)
    v = _f((((state or {}).get("clearinghouse") or {}).get("marginSummary") or {}).get("accountValue"))
    return v or None


async def sync_cached(s: Session, hl: Hyperliquid, settings: Settings, now_ms: int, max_age_ms: int = 60_000
                      ) -> dict[str, Any] | None:
    st = kv_get(s, key(settings.moonshot_wallet)) if settings.moonshot_wallet else None
    if st and now_ms - int(st.get("updated_ms") or 0) < max_age_ms:
        return dict(st)
    return await sync(s, hl, settings, now_ms)


def view(s: Session, settings: Settings, main_value: float | None = None) -> dict[str, Any]:
    addr = settings.moonshot_wallet
    st = kv_get(s, key(addr)) if addr else None
    out: dict[str, Any] = {"address": addr, "budget_setting": settings.moonshot_budget_usd, "rules": RULES,
                           "state": st, "warnings": []}
    if not st:
        return out
    budget = st.get("budget") or 0.0
    pnl = st.get("pnl") or 0.0
    out["left_pct"] = max(0.0, (budget + pnl) / budget) if budget else None
    out["pnl_pct"] = pnl / budget if budget else None
    w = out["warnings"]
    if st.get("topups"):
        last = st["topups"][-1]
        w.append(f"You added ${last['usd']:,.0f} while the bucket was down ${-last['pnl_before']:,.0f}. That breaks "
                 "rule 2: a top-up after a loss turns a small bet into a big one.")
    if budget and budget + pnl <= 0.1 * budget:
        w.append("The bucket is (almost) empty. Rule 4: stop here and don't refill it this month.")
    elif budget and pnl <= -0.5 * budget:
        w.append("Half the bucket is gone. Slow down: smaller size, and only trades with a stop.")
    if budget and pnl >= budget:
        w.append(f"It has doubled. Rule 3: withdraw your original ${budget:,.0f} now and play only with the profit.")
    no_stop = [p["coin"] for p in st.get("positions") or [] if not p.get("has_stop")]
    if no_stop:
        w.append("No stop on: " + ", ".join(no_stop) + ". Set one now (rule 5).")
    if main_value and st.get("value"):
        share = st["value"] / (main_value + st["value"])
        out["share"] = share
        if share > SHARE_WARN:
            w.append(f"The moonshot is {share:.0%} of everything you track. Keep it at 10% or less.")
    return out


async def alerts(s: Session, settings: Settings, st: dict[str, Any] | None) -> list[str]:
    """Telegram once per event: half gone, empty, doubled, top-up after a loss."""
    from perpdesk.alerts import telegram

    if not st or st.get("error") or not settings.moonshot_wallet:
        return []
    budget, pnl = st.get("budget") or 0.0, st.get("pnl") or 0.0
    sent = dict(st.get("alerted") or {})
    msgs: list[tuple[str, str]] = []
    if budget:
        if pnl <= -0.5 * budget:
            msgs.append(("half", f"🎲 Moonshot: half the bucket is gone (${budget + pnl:,.0f} of ${budget:,.0f} "
                                 "left). Smaller size, stops on everything."))
        if budget + pnl <= 0.1 * budget:
            msgs.append(("empty", "🎲 Moonshot bucket is empty. You agreed: stop for this month, no refill."))
        level = int(pnl // budget) if pnl >= budget else 0
        if level >= 1:
            msgs.append((f"double{level}", f"🎲 Moonshot is up {pnl / budget:.0%}. Take your original "
                                            f"${budget:,.0f} out now and play only with the profit."))
    for tp in st.get("topups") or []:
        msgs.append((f"topup{tp['ms']}", f"🎲 You topped up the moonshot (${tp['usd']:,.0f}) after a loss. That "
                                         "breaks the rule that keeps this a small bet."))
    out = []
    for k, text in msgs:
        if sent.get(k):
            continue
        if await telegram.notify(s, settings, text):
            sent[k] = True
            out.append(k)
    if out:
        st["alerted"] = sent
        kv_set(s, key(settings.moonshot_wallet), st)
    return out
