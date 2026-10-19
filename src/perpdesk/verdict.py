"""One TAKE / SKIP verdict per coin card.

TAKE means: a proven paper bot (breakout, trend follower, funding carry) has a live signal on this coin that is
fresh (< 30 min), price is still near its entry, no market-wide warning is active and the card itself is not
stale. Everything else is SKIP with one main reason. Grade and confidence never make a card TAKE on their own;
the composite plan only becomes TAKE when take_hint() says it has a statistically solid live record.

Also: next support / resistance and a "room to move" check (is a strong level sitting between entry and target).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from perpdesk.config import Settings
from perpdesk.plan import KEY_LEVELS, TARGET_KINDS

FRESH_MS = 30 * 60_000
STALE_MS = 90 * 60_000
ROOM_BLOCK_R = 0.5
# Flags that describe the market, not the composite plan, so they apply to bot signals too.
MARKET_FLAGS = {"event", "volatility", "stale", "liquidity"}
# Flags tied to the composite direction: they only apply when the bot trades the same way.
DIRECTION_FLAGS = {"btc_counter", "crowded"}

_MULT = re.compile(r"×(\d+)")


@dataclass(frozen=True)
class Lvl:
    price: float
    kind: str
    label: str
    strength: int

    @property
    def significant(self) -> bool:
        return self.kind in KEY_LEVELS or (self.kind in TARGET_KINDS and self.strength >= 2)


def _strength(kind: str, label: str) -> int:
    """Older views store (price, kind, label) only: read the touch count back out of the label."""
    m = _MULT.search(label or "")
    n = int(m.group(1)) if m else (2 if kind in KEY_LEVELS else 1)
    return n + 1 if "(4H)" in (label or "") else n


def levels_from_view(v: dict[str, Any]) -> list[Lvl]:
    out = []
    for row in v.get("levels") or []:
        try:
            price, kind, label = float(row[0]), str(row[1]), str(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        st = int(row[3]) if len(row) > 3 and row[3] is not None else _strength(kind, label)
        out.append(Lvl(price, kind, label, st))
    return out


def next_levels(levels: list[Lvl], price: float) -> tuple[Lvl | None, Lvl | None]:
    """Nearest significant level below (support) and above (resistance) the price, whatever its original kind."""
    sig = [lv for lv in levels if lv.significant and lv.price > 0]
    below = max((lv for lv in sig if lv.price < price), key=lambda lv: lv.price, default=None)
    above = min((lv for lv in sig if lv.price > price), key=lambda lv: lv.price, default=None)
    return below, above


@dataclass
class Room:
    status: str  # ok | caution | block | n/a
    reason: str
    level: Lvl | None = None
    dist_r: float | None = None


def room_check(direction: str, entry: float, sl: float, tp1: float | None, levels: list[Lvl]) -> Room:
    """block: a significant level against the trade sits within 0.5R of entry (shorting into support / buying
    under resistance). caution: one sits between entry and TP1. Same rule in the backtest (lab/room.py)."""
    if direction not in ("LONG", "SHORT") or not entry or not sl or entry == sl:
        return Room("n/a", "")
    r = abs(entry - sl)
    d = 1 if direction == "LONG" else -1
    ahead = [lv for lv in levels if lv.significant and (lv.price - entry) * d > 0]
    if not ahead:
        return Room("ok", "no strong level in the way")
    lv = min(ahead, key=lambda x: abs(x.price - entry))
    dist = abs(lv.price - entry) / r
    side = "support" if d < 0 else "resistance"
    name = lv.label or side
    if dist < ROOM_BLOCK_R:
        return Room("block", f"{'shorting into' if d < 0 else 'buying under'} {name} at {lv.price:.6g} "
                             f"({dist:.1f}R away)", lv, dist)
    if tp1 and (tp1 - lv.price) * d > 0:
        return Room("caution", f"{name} at {lv.price:.6g} sits before TP1 ({dist:.1f}R away)", lv, dist)
    return Room("ok", f"next {side} {dist:.1f}R away, beyond TP1" if tp1 else f"next {side} {dist:.1f}R away",
                lv, dist)


@dataclass
class Verdict:
    take: bool
    reason: str
    source: str  # bot | plan | none
    bot: dict[str, Any] | None
    room: Room
    support: dict[str, Any] | None
    resistance: dict[str, Any] | None
    take_until_ms: int | None
    cautions: list[str]


def _iso_ms(x: str | None) -> int | None:
    if not x:
        return None
    try:
        return int(datetime.fromisoformat(x).timestamp() * 1000)
    except ValueError:
        return None


def _lvl_view(lv: Lvl | None, price: float, r: float | None) -> dict[str, Any] | None:
    if lv is None or not price:
        return None
    return {"price": lv.price, "label": lv.label, "pct": lv.price / price - 1,
            "r": abs(lv.price - price) / r if r else None}


def bot_view(t: Any, labels: dict[str, Any]) -> dict[str, Any]:
    return {"bot": t.setup, "label": labels.get(t.setup, {}).get("label", t.setup), "direction": t.direction,
            "entry": t.fill_px or t.entry, "sl": t.stop_now or t.sl, "tp1": t.tp1 or None, "tp2": t.tp2 or None,
            "exit_model": t.exit_model, "status": t.status, "created_ms": t.created_ms, "note": t.note or "",
            "age_min": None}


def verdict(v: dict[str, Any], bot: Any | None, now_ms: int, settings: Settings,
            labels: dict[str, Any] | None = None) -> Verdict:
    """v: a LatestView payload. bot: the newest pending/open paper trade of an enabled bot on this coin."""
    labels = labels or {}
    price = float(v.get("price") or 0.0)
    levels = levels_from_view(v)
    plan = v.get("plan") or {}
    flags = {f.get("code"): f.get("reason", "") for f in v.get("flags") or []}
    updated = _iso_ms(v.get("updated_at"))
    stale = updated is None or now_ms - updated > STALE_MS
    bv = bot_view(bot, labels) if bot is not None else None
    if bv:
        bv["age_min"] = max(0, round((now_ms - bv["created_ms"]) / 60_000))

    if bv and bv["direction"] in ("LONG", "SHORT"):
        direction, entry, sl, tp1 = bv["direction"], bv["entry"], bv["sl"], bv["tp1"]
    else:
        direction, entry, sl, tp1 = v.get("direction"), plan.get("entry"), plan.get("sl"), plan.get("tp1")
    r = abs(entry - sl) if entry and sl and entry != sl else None
    sup, res = next_levels(levels, price)
    room = room_check(direction or "", entry or 0.0, sl or 0.0, tp1, levels)
    sv, rv = _lvl_view(sup, price, r), _lvl_view(res, price, r)
    cautions = [room.reason] if room.status in ("caution", "block") else []

    def mk(take: bool, reason: str, source: str, b: dict[str, Any] | None, until: int | None,
           cs: list[str]) -> Verdict:
        return Verdict(take, reason, source, b, room, sv, rv, until, cs)

    def skip(reason: str, source: str = "none") -> Verdict:
        return mk(False, reason, source, bv, None, cautions)

    if stale:
        return skip("card is stale (last analysed over 90 min ago) — press ↻ first")
    if bv is None:
        if v.get("take") and not flags and room.status != "block" and plan.get("sl"):
            return mk(True, v.get("take_reason") or "proven live record", "plan", None, (updated or now_ms) + FRESH_MS,
                      cautions)
        if flags:
            return skip("⚠ " + next(iter(flags.values())))
        return skip("no proven-bot signal on this coin — the general analysis alone lost money in testing")

    blocking = {k: x for k, x in flags.items()
                if k in MARKET_FLAGS or (k in DIRECTION_FLAGS and v.get("direction") == bv["direction"])}
    age = now_ms - bv["created_ms"]
    if age > FRESH_MS:
        return skip(f"{bv['label']} signal is {bv['age_min']} min old — only take one within 30 min", "bot")
    if blocking:
        return skip("⚠ " + next(iter(blocking.values())), "bot")
    if bv["exit_model"] == "carry":
        return mk(True, f"{bv['label']}: funding carry opened {bv['age_min']} min ago", "bot", bv,
                  bv["created_ms"] + FRESH_MS, [])
    if bv["status"] == "open" and getattr(bot, "tp1_hit", False):
        return skip("bot already took profit at TP1 — too late to enter", "bot")
    if r and price:
        d = 1 if direction == "LONG" else -1
        drift = (price - entry) * d / r
        if (price - sl) * d <= 0:
            return skip("price is already past the bot's stop", "bot")
        if abs(drift) > settings.auto_max_entry_drift_r:
            return skip(f"price has moved {drift:+.1f}R from the bot's entry (limit "
                        f"±{settings.auto_max_entry_drift_r:g}R)", "bot")
    if room.status == "block" and settings.room_check_filter:
        return skip("no room: " + room.reason, "bot")
    return mk(True, f"{bv['label']} {direction} signal, {bv['age_min']} min old, price near entry", "bot", bv,
              bv["created_ms"] + FRESH_MS, cautions)
