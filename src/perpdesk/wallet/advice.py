"""Assisted mode: compare each open position (and its TP/SL orders) with the app's current plan and say
what to change. Pure functions; the user makes every change themselves."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class Position:
    coin: str
    direction: str
    size: float
    entry: float
    mark: float | None
    liq: float | None
    leverage: int | None
    margin_type: str
    margin: float | None
    upnl: float | None
    roe: float | None
    funding_since_open: float | None  # positive = received
    stops: list[float] = field(default_factory=list)
    targets: list[float] = field(default_factory=list)

    @property
    def stop(self) -> float | None:
        """Tightest stop (closest to the mark/entry)."""
        if not self.stops:
            return None
        return max(self.stops) if self.direction == "LONG" else min(self.stops)

    @property
    def target(self) -> float | None:
        if not self.targets:
            return None
        return min(self.targets) if self.direction == "LONG" else max(self.targets)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(stop=self.stop, target=self.target, notional=abs(self.size) * (self.mark or self.entry))
        return d


def parse_positions(state: dict[str, Any], orders: list[dict[str, Any]]) -> list[Position]:
    out = []
    for ap in state.get("assetPositions") or []:
        p = ap.get("position") or {}
        szi = _f(p.get("szi")) or 0.0
        if szi == 0:
            continue
        coin = str(p.get("coin"))
        entry = _f(p.get("entryPx")) or 0.0
        value = _f(p.get("positionValue"))
        lev = p.get("leverage") or {}
        cum = (p.get("cumFunding") or {}).get("sinceOpen")
        pos = Position(
            coin=coin, direction="LONG" if szi > 0 else "SHORT", size=abs(szi), entry=entry,
            mark=(value / abs(szi)) if value else None, liq=_f(p.get("liquidationPx")),
            leverage=int(lev["value"]) if lev.get("value") is not None else None,
            margin_type=str(lev.get("type", "")), margin=_f(p.get("marginUsed")), upnl=_f(p.get("unrealizedPnl")),
            roe=_f(p.get("returnOnEquity")), funding_since_open=-c if (c := _f(cum)) is not None else None,
        )
        closing_side = "A" if pos.direction == "LONG" else "B"
        for o in orders:
            if o.get("coin") != coin or o.get("side") != closing_side:
                continue
            otype = str(o.get("orderType", ""))
            trig = _f(o.get("triggerPx")) if o.get("isTrigger") else None
            px = trig or _f(o.get("limitPx"))
            if px is None:
                continue
            if "Stop" in otype:
                pos.stops.append(px)
            elif "Take Profit" in otype:
                pos.targets.append(px)
            elif o.get("reduceOnly") or o.get("isPositionTpsl"):
                beyond = px > entry if pos.direction == "LONG" else px < entry
                (pos.targets if beyond else pos.stops).append(px)
        out.append(pos)
    return out


@dataclass
class Advice:
    level: str  # danger|warn|info|ok
    text: str


def _fmt(x: float | None) -> str:
    if x is None:
        return "–"
    a = abs(x)
    return f"{x:,.1f}" if a >= 1000 else f"{x:,.2f}" if a >= 100 else f"{x:.4g}"


def advise(pos: Position, plan_view: dict[str, Any] | None, risk_pct_of_margin: float) -> list[Advice]:
    out: list[Advice] = []
    sign = 1 if pos.direction == "LONG" else -1
    pv = plan_view or {}
    plan = pv.get("plan") if not pv.get("error") else None
    same = bool(plan) and pv.get("direction") == pos.direction
    stop, tgt, mark = pos.stop, pos.target, pos.mark or pos.entry

    if plan and not same:
        out.append(Advice("warn", f"The app now calls {pv.get('direction')} (grade {pv.get('grade')}, "
                                  f"confidence {pv.get('confidence')}). Your {pos.direction} is against the "
                                  "current signal: consider closing it or tightening the stop."))
    if stop is None:
        hint = f" The plan's stop is {_fmt(plan['sl'])}." if same and plan else ""
        out.append(Advice("danger", f"No stop-loss order on this position.{hint} Add a reduce-only Stop Market order."))
    else:
        if pos.liq and sign * (stop - pos.liq) <= 0:
            out.append(Advice("danger", f"Liquidation ({_fmt(pos.liq)}) comes before your stop ({_fmt(stop)}). "
                                        "Lower leverage, add margin, or move the stop inside the liquidation price."))
        elif pos.liq and pos.entry and abs(pos.liq - pos.entry) < 1.5 * abs(pos.entry - stop):
            out.append(Advice("warn", f"Liquidation ({_fmt(pos.liq)}) is close behind your stop ({_fmt(stop)}): a "
                                      "wick or slippage past the stop could liquidate you. Use less leverage."))
        loss = abs(pos.entry - stop) * pos.size if sign * (pos.entry - stop) > 0 else 0.0
        if pos.margin and loss > 0:
            pct = loss / pos.margin * 100
            if pct > risk_pct_of_margin * 1.5:
                out.append(Advice("warn", f"Hitting your stop loses about ${loss:,.2f} = {pct:.0f}% of this position's "
                                          f"margin (the app sizes for {risk_pct_of_margin:g}%)."))
        if tgt is not None and sign * (pos.entry - stop) > 0:
            rr = abs(tgt - pos.entry) / abs(pos.entry - stop)
            if rr < 1:
                be = 1 / (1 + rr)
                p_hint = f" Plan targets: TP1 {_fmt(plan['tp1'])}, TP2 {_fmt(plan['tp2'])}." if same and plan else ""
                out.append(Advice("warn", f"Your target is closer than your stop (R:R {rr:.2f}): you need about "
                                          f"{be:.0%} wins just to break even, before fees.{p_hint}"))
    if same and plan:
        tp1 = float(plan["tp1"])
        if sign * (mark - tp1) >= 0 and (stop is None or sign * (stop - pos.entry) < 0):
            out.append(Advice("warn", f"Price is past the plan's TP1 ({_fmt(tp1)}). The plan takes half off there and "
                                      f"moves the stop to break-even ({_fmt(pos.entry)})."))
        if tgt is None:
            out.append(Advice("info", f"No take-profit order. Plan: TP1 {_fmt(tp1)}, TP2 {_fmt(plan['tp2'])}."))
        if pos.leverage and plan.get("leverage") and pos.leverage > int(plan["leverage"]) * 1.5:
            out.append(Advice("info", f"You use {pos.leverage}x; the plan suggests {plan['leverage']}x for this stop "
                                      "distance. More leverage moves liquidation closer without improving the trade."))
    if pos.margin_type and pos.margin_type != "isolated":
        out.append(Advice("info", "This position uses cross margin; the app's plans assume isolated margin."))
    if not out:
        out.append(Advice("ok", "Stop and target are set and consistent with the current plan."))
    return out
