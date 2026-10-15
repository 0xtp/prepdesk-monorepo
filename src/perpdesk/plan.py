"""Trade plan construction: entry, stop, targets, size, leverage, liquidation check, costs, grade.

Every number shown to the user comes from here. Pure functions, no I/O.

Sizing rule: size from risk, never from leverage.
    qty = risk_usd / (net loss per coin if the stop is hit, incl. fees, slippage and funding)
    leverage = ceil(notional / margin), capped by MAX_LEVERAGE, the asset's max leverage and the
    liquidation-buffer check. If a cap binds, the notional shrinks (risk goes down, never up).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from perpdesk.analysis.setups import SETUP_LABELS
from perpdesk.analysis.structure import nearest
from perpdesk.analysis.types import TF_MS, Analysis, AssetMeta, Direction, Flag, Grade, Level

HL_MIN_NOTIONAL_USD = 10.0
SL_KINDS = {"support", "resistance", "pdl", "pdh", "pwl", "pwh", "eql", "eqh", "val", "vah"}
ENTRY_KINDS = {"vwap", "ema", "support", "resistance", "fvg", "poc", "val", "vah", "pdl", "pdh"}
TARGET_KINDS = {"support", "resistance", "pdl", "pdh", "pwl", "pwh", "eql", "eqh"}
KEY_LEVELS = {"pdl", "pdh", "pwl", "pwh", "eql", "eqh"}


def is_target(lv: Level) -> bool:
    """Targets respect only significant opposing levels: prior day/week extremes, liquidity pools and
    support/resistance touched at least twice. Minor levels (VWAP, POC, single touches) are not targets."""
    return lv.kind in KEY_LEVELS or (lv.kind in TARGET_KINDS and lv.strength >= 2)


@dataclass(frozen=True)
class PlanConfig:
    min_rr: float = 1.5
    min_confidence: int = 60
    grade_a_size: float = 1.0
    grade_b_size: float = 0.5
    grade_c_size: float = 0.25
    caution_size: float = 0.5
    sl_atr_buffer: float = 0.3
    min_sl_atr: float = 0.8
    max_sl_atr: float = 3.0
    min_liq_buffer_atr: float = 3.0
    max_leverage: int = 10
    plan_ttl_min: int = 60
    max_hold_hours: int = 48
    taker_fee_bps: float = 4.5
    maker_fee_bps: float = 1.5
    slippage_bps: float = 3.0
    expected_hold_bars: int = 12


@dataclass(frozen=True)
class RiskInputs:
    margin_usd: float
    risk_usd: float  # base risk before grade/caution multipliers

    def __post_init__(self) -> None:
        if self.margin_usd <= 0 or self.risk_usd <= 0:
            raise ValueError("margin and risk must be positive")


@dataclass(frozen=True)
class WinStats:
    p: float  # calibrated P(TP1 before SL)
    lo: float
    hi: float
    n: int
    scope: str  # e.g. "trend_pullback · SOL · LONG" or "trend_pullback · Large · LONG"
    low_sample: bool
    expectancy_r: float | None = None
    pf: float | None = None
    win_rate: float | None = None
    max_dd_r: float | None = None


@dataclass(frozen=True)
class GateInfo:
    passed: bool = False
    demoted_reason: str | None = None
    stats: WinStats | None = None
    proof: str | None = None  # "Trend-pullback · SOL · LONG — 214 OOS trades, win 47%, ..."
    live_proof: str | None = None


@dataclass
class Plan:
    coin: str
    direction: Direction
    grade: Grade
    setup: str
    setup_label: str
    confidence: int
    expected_move: str
    spot_only: bool
    entry_type: Literal["limit", "market"]
    entry_market: float
    entry_zone: tuple[float, float]
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp_split: tuple[float, float]
    r_gross: float
    rr_tp1: float
    rr_tp2: float
    risk_usd_target: float
    risk_usd: float
    notional: float
    qty: float
    leverage: int
    margin_used: float
    liq_price: float | None
    liq_buffer_atr: float | None
    fees_usd: float
    funding_usd: float
    usd_tp1: float
    usd_tp2_total: float
    flags: list[Flag]
    invalidation: str
    entry_note: str
    ttl_min: int
    created_at_ms: int
    expires_at_ms: int
    runaway_price: float
    win: WinStats | None
    break_even: float
    ev_usd: float | None
    ev_basis: str
    proof: str | None
    live_proof: str | None
    notes: list[str] = field(default_factory=list)

    def copy_text(self) -> str:
        side = self.direction
        unit = self.coin
        lines = [
            f"{self.coin}{'' if self.spot_only else '-PERP'} {side} · Grade {self.grade}"
            + (" ⚠" if self.flags else ""),
            f"Entry: {'market ' + fmt_px(self.entry_market) if self.entry_type == 'market' else 'limit '}"
            + (f"{fmt_px(self.entry_zone[0])}–{fmt_px(self.entry_zone[1])}" if self.entry_type == "limit" else ""),
            f"SL: {fmt_px(self.sl)}  TP1: {fmt_px(self.tp1)} ({self.tp_split[0]:.0%})  "
            f"TP2: {fmt_px(self.tp2)} ({self.tp_split[1]:.0%})  → SL to breakeven after TP1",
            (f"Size: {self.qty:g} {unit}" if self.spot_only else
             f"Size: {self.qty:g} {unit} · {self.leverage}x isolated · margin ${self.margin_used:,.2f}"),
            f"Risk ${self.risk_usd:,.2f} · R:R {self.rr_tp1:.2f} to TP1 after fees",
        ]
        if self.liq_price:
            lines.append(f"Est. liq {fmt_px(self.liq_price)}")
        elif self.liq_price == 0.0:
            lines.append("Est. liq: none (1x long)")
        lines.append(f"Invalid if: {self.invalidation}")
        return "\n".join(lines)


# ------------------------------------------------------------------ price / size rounding


def fmt_px(x: float) -> str:
    if x >= 1000:
        return f"{x:,.1f}"
    return f"{x:.6g}"


def round_price(px: float, sz_decimals: int, mode: Literal["near", "down", "up"] = "near", spot: bool = False) -> float:
    """Hyperliquid perp tick rules: at most 5 significant figures and (6 - szDecimals) decimals; integer prices
    are always valid. Spot / DEX prices (spot=True) are rounded to 5 significant figures only."""
    if px <= 0 or not math.isfinite(px):
        raise ValueError(f"bad price {px}")
    max_dec = 30 if spot else 6 - sz_decimals
    d = Decimal(repr(px))
    exp = d.adjusted()  # power of ten of the leading digit
    sig_dec = max(0, 5 - 1 - exp)  # decimals allowed by 5 significant figures
    decimals = max(0, min(max_dec, sig_dec))
    q = Decimal(1).scaleb(-decimals)
    if decimals == 0 and exp >= 5:
        q = Decimal(1)  # integers always allowed
    rounding = {"near": "ROUND_HALF_EVEN", "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
    return float(d.quantize(q, rounding=rounding))


def floor_size(qty: float, sz_decimals: int) -> float:
    q = Decimal(1).scaleb(-sz_decimals)
    return float(Decimal(repr(max(qty, 0.0))).quantize(q, rounding=ROUND_FLOOR))


# ------------------------------------------------------------------ margin / liquidation


def tier_for(notional: float, tiers: list[tuple[float, int]], default_max: int) -> int:
    lev = default_max
    for lower, mx in sorted(tiers):
        if notional >= lower:
            lev = mx
    return lev


def maintenance_margin(notional: float, tiers: list[tuple[float, int]], default_max: int) -> float:
    """Piecewise maintenance margin: rate = 1/(2*tier max leverage), continuous across tier bounds."""
    if not tiers:
        return notional / (2 * default_max)
    ts = sorted(tiers)
    mm = 0.0
    for i, (lower, mx) in enumerate(ts):
        upper = ts[i + 1][0] if i + 1 < len(ts) else math.inf
        if notional <= lower:
            break
        mm += (min(notional, upper) - lower) / (2 * mx)
    return mm


def liquidation_price(entry: float, qty: float, margin: float, direction: Direction,
                      tiers: list[tuple[float, int]], default_max: int) -> float:
    """Isolated-margin liquidation: account value (margin + uPnL) == maintenance margin at mark."""
    if qty <= 0 or margin <= 0:
        raise ValueError("qty and margin must be positive")

    def gap(p: float) -> float:
        pnl = qty * (p - entry) if direction == "LONG" else qty * (entry - p)
        return margin + pnl - maintenance_margin(qty * p, tiers, default_max)

    if direction == "LONG":
        lo, hi = 0.0, entry
        if gap(lo) > 0:
            return 0.0
    else:
        lo, hi = entry, entry * 2
        while gap(hi) > 0:
            hi *= 2
            if hi > entry * 1e6:
                return math.inf
    for _ in range(200):
        mid = (lo + hi) / 2
        g = gap(mid)
        if direction == "LONG":  # gap rises with price; liquidated below the root
            lo, hi = (mid, hi) if g < 0 else (lo, mid)
        else:  # gap falls with price; liquidated above the root
            lo, hi = (mid, hi) if g > 0 else (lo, mid)
    return (lo + hi) / 2


def liquidation_price_simple(entry: float, leverage: float, direction: Direction, max_leverage: int) -> float:
    """Closed form for a single margin tier (used to cross-check the general solver)."""
    l_rate = 1 / (2 * max_leverage)
    if direction == "LONG":
        return entry * (1 - 1 / leverage) / (1 - l_rate)
    return entry * (1 + 1 / leverage) / (1 + l_rate)


# ------------------------------------------------------------------ costs


@dataclass(frozen=True)
class Costs:
    entry_fee_rate: float
    exit_fee_rate: float
    slippage_rate: float
    funding_rate_per_hour: float
    hold_hours: float


def per_unit_loss(entry: float, sl: float, c: Costs, dsign: int) -> float:
    """Loss per coin if stopped: distance + entry fee + stop exit fee + slippage on the stop + funding paid."""
    funding = max(0.0, dsign * c.funding_rate_per_hour * c.hold_hours) * entry
    return abs(entry - sl) + entry * c.entry_fee_rate + sl * (c.exit_fee_rate + c.slippage_rate) + funding


def per_unit_gain(entry: float, tp: float, c: Costs, dsign: int) -> float:
    funding = max(0.0, dsign * c.funding_rate_per_hour * c.hold_hours) * entry
    return abs(tp - entry) - entry * c.entry_fee_rate - tp * c.exit_fee_rate - funding


def net_rr(entry: float, sl: float, tp: float, c: Costs, dsign: int) -> float:
    loss = per_unit_loss(entry, sl, c, dsign)
    return per_unit_gain(entry, tp, c, dsign) / loss if loss > 0 else 0.0


# ------------------------------------------------------------------ level selection


def _entry_zone(a: Analysis, dsign: int, atr: float) -> tuple[float, float, str]:
    price = a.price
    cand = [lv for lv in a.levels if lv.kind in ENTRY_KINDS]
    side: Literal["below", "above"] = "below" if dsign > 0 else "above"
    near = nearest(cand, price, side, min_dist=0.15 * atr, max_dist=1.0 * atr)
    if near:
        lv = near[0]
        mid, label = lv.price, lv.label or lv.kind
    else:
        mid, label = price - dsign * 0.3 * atr, "0.3 ATR pullback"
    lo, hi = mid - 0.1 * atr, mid + 0.1 * atr
    # never let the zone straddle current price
    if dsign > 0:
        hi = min(hi, price)
    else:
        lo = max(lo, price)
    return lo, hi, label


def _stop(a: Analysis, entry: float, dsign: int, atr: float, cfg: PlanConfig) -> tuple[float, float, str]:
    """Returns (stop price, invalidating structure level, description)."""
    side: Literal["below", "above"] = "below" if dsign > 0 else "above"
    struct = [lv for lv in a.levels if lv.kind in SL_KINDS]
    struct += [Level(p.price, "pivot", 1, f"swing {'low' if p.kind == 'low' else 'high'}") for p in a.pivots
               if (p.kind == "low") == (dsign > 0)]
    min_d = max(0.0, cfg.min_sl_atr * atr - cfg.sl_atr_buffer * atr)
    cand = nearest(struct, entry, side, min_dist=min_d, max_dist=cfg.max_sl_atr * atr)
    if cand:
        lv = cand[0]
        sl = lv.price - dsign * cfg.sl_atr_buffer * atr
        ref, desc = lv.price, lv.label or lv.kind
    else:
        sl = entry - dsign * 1.5 * atr
        ref, desc = sl, "1.5 ATR (no structure in range)"
    if abs(entry - sl) < cfg.min_sl_atr * atr:
        sl = entry - dsign * cfg.min_sl_atr * atr
        desc += f", widened to {cfg.min_sl_atr:g} ATR minimum"
    return sl, ref, desc


def _targets(a: Analysis, entry: float, r: float, dsign: int, rr_target_gross: float, skip_first: bool
             ) -> tuple[float, float]:
    side: Literal["below", "above"] = "above" if dsign > 0 else "below"
    opp = nearest([lv for lv in a.levels if is_target(lv)], entry, side, min_dist=0.5 * r)
    target = entry + dsign * rr_target_gross * r
    pool = opp[1:] if skip_first else opp
    tp1 = pool[0].price if pool else target
    tp1 = min(tp1, target) if dsign > 0 else max(tp1, target)
    beyond = [lv for lv in opp if (lv.price - tp1) * dsign > 0.25 * r]
    # TP2 sits at 2.5–3R, but always at least 0.5R beyond TP1 (matters when MIN_RR is set above 2.5)
    d1 = abs(tp1 - entry) / r if r > 0 else 0.0
    m_lo = max(2.5, d1 + 0.5)
    m_hi = max(3.0, m_lo)
    lo2, hi2 = entry + dsign * m_lo * r, entry + dsign * m_hi * r
    tp2 = beyond[0].price if beyond else lo2
    tp2 = min(max(tp2, lo2), hi2) if dsign > 0 else max(min(tp2, lo2), hi2)
    return tp1, tp2


def _gross_multiple_for_net(entry: float, sl: float, c: Costs, dsign: int, rr_net: float) -> float:
    """Gross distance (in R) a target needs so that the net R:R equals rr_net."""
    r = abs(entry - sl)
    loss = per_unit_loss(entry, sl, c, dsign)
    funding = max(0.0, dsign * c.funding_rate_per_hour * c.hold_hours) * entry
    # gain = d - entry*fe - (entry + dsign*d)*fx - funding = rr_net*loss  ->  solve for d
    d = (rr_net * loss + entry * c.entry_fee_rate + entry * c.exit_fee_rate + funding) / (1 - dsign * c.exit_fee_rate)
    return d / r if r > 0 else rr_net


# ------------------------------------------------------------------ main


def build_plan(a: Analysis, meta: AssetMeta, risk: RiskInputs, cfg: PlanConfig, gate: GateInfo | None = None,
               current_funding: float = 0.0, extra_flags: list[Flag] | None = None) -> Plan:
    gate = gate or GateInfo()
    dsign = 1 if a.direction == "LONG" else -1
    atr = a.atr
    if not (math.isfinite(atr) and atr > 0):
        raise ValueError("ATR unavailable")
    flags: list[Flag] = list(a.flags) + list(extra_flags or [])
    notes: list[str] = list(a.notes)
    spot = not meta.is_perp
    price = a.price
    hold_hours = min(cfg.max_hold_hours, cfg.expected_hold_bars * TF_MS[a.trading_tf] / 3_600_000)

    def costs(entry_type: str) -> Costs:
        return Costs(
            entry_fee_rate=(cfg.maker_fee_bps if entry_type == "limit" else cfg.taker_fee_bps + cfg.slippage_bps) / 1e4,
            exit_fee_rate=cfg.taker_fee_bps / 1e4,
            slippage_rate=cfg.slippage_bps / 1e4,
            funding_rate_per_hour=0.0 if spot else current_funding,
            hold_hours=hold_hours,
        )

    range_trade = a.setup.name == "composite" and a.expected_move == "SIDEWAYS" and a.range is not None
    entry_type: Literal["limit", "market"] = a.setup.entry_type
    if range_trade and a.range is not None:
        width = a.range.high - a.range.low
        if dsign > 0:
            zlo, zhi = a.range.low, min(price, a.range.low + 0.2 * width)
        else:
            zlo, zhi = max(price, a.range.high - 0.2 * width), a.range.high
        if zhi <= zlo:  # price already beyond the edge
            zlo, zhi = (price - 0.1 * atr, price) if dsign > 0 else (price, price + 0.1 * atr)
        zone_label = "range " + ("low" if dsign > 0 else "high")
        entry_type = "limit"
    else:
        zlo, zhi, zone_label = _entry_zone(a, dsign, atr)

    def levels_for(et: str) -> tuple[float, float, float, str, float, float, float]:
        entry = price if et == "market" else (zlo + zhi) / 2
        if range_trade and a.range is not None:
            edge = a.range.low if dsign > 0 else a.range.high
            sl = edge - dsign * cfg.sl_atr_buffer * atr
            if abs(entry - sl) < cfg.min_sl_atr * atr:
                sl = entry - dsign * cfg.min_sl_atr * atr
            ref, sl_desc = edge, "range " + ("low" if dsign > 0 else "high")
        else:
            sl, ref, sl_desc = _stop(a, entry, dsign, atr, cfg)
        r = abs(entry - sl)
        c = costs(et)
        if range_trade and a.range is not None:
            tp1 = a.range.mid
            tp2 = (a.range.high - 0.1 * atr) if dsign > 0 else (a.range.low + 0.1 * atr)
            if (tp1 - entry) * dsign <= 0:
                tp1 = entry + dsign * 1.5 * r
            if (tp2 - tp1) * dsign <= 0:
                tp2 = tp1 + dsign * r
        else:
            # small cushion so rounding targets toward entry never drops R:R below the minimum
            target_mult = _gross_multiple_for_net(entry, sl, c, dsign, max(1.5, cfg.min_rr) + 0.03)
            tp1, tp2 = _targets(a, entry, r, dsign, target_mult, skip_first=False)
            if net_rr(entry, sl, tp1, c, dsign) < cfg.min_rr:
                tp1b, tp2b = _targets(a, entry, r, dsign, target_mult, skip_first=True)
                if net_rr(entry, sl, tp1b, c, dsign) > net_rr(entry, sl, tp1, c, dsign):
                    tp1, tp2 = tp1b, tp2b
        return entry, sl, ref, sl_desc, tp1, tp2, r

    entry, sl, sl_ref, sl_desc, tp1, tp2, r = levels_for(entry_type)
    rr1 = net_rr(entry, sl, tp1, costs(entry_type), dsign)
    if rr1 < cfg.min_rr and entry_type == "market":
        alt = levels_for("limit")
        alt_rr = net_rr(alt[0], alt[1], alt[4], costs("limit"), dsign)
        if alt_rr > rr1:
            notes.append("Switched to limit entry to improve R:R")
            entry_type = "limit"
            entry, sl, sl_ref, sl_desc, tp1, tp2, r = alt
            rr1 = alt_rr

    # rounding to valid prices: stop away from entry (rounded after sizing uses exact values -> conservative),
    # targets toward entry.
    sd = meta.sz_decimals
    entry_r = round_price(entry, sd, spot=spot)
    sl_r = round_price(sl, sd, "down" if dsign > 0 else "up", spot=spot)
    tp1_r = round_price(tp1, sd, "down" if dsign > 0 else "up", spot=spot)
    tp2_r = round_price(tp2, sd, "down" if dsign > 0 else "up", spot=spot)
    zlo_r, zhi_r = round_price(zlo, sd, spot=spot), round_price(zhi, sd, spot=spot)
    entry, sl, tp1, tp2 = entry_r, sl_r, tp1_r, tp2_r
    if (entry - sl) * dsign <= 0:
        raise ValueError("stop on wrong side of entry after rounding")
    if (tp1 - entry) * dsign <= 0 or (tp2 - tp1) * dsign < 0:
        raise ValueError("price tick too coarse for this volatility: targets collapse onto entry after rounding")
    c = costs(entry_type)
    rr1 = net_rr(entry, sl, tp1, c, dsign)
    rr2 = net_rr(entry, sl, tp2, c, dsign)

    # ---- grade
    grade: Grade
    if a.near_zero:
        grade = "C"
        notes.append("Near-zero score: direction from higher-timeframe trend")
    elif a.setup.name != "composite" and gate.passed and gate.demoted_reason is None:
        grade = "A"
    else:
        grade = "B" if a.confidence >= cfg.min_confidence else "C"
    if gate.demoted_reason:
        notes.append(f"Demoted from A: {gate.demoted_reason}")
    if rr1 < cfg.min_rr:
        flags.append(Flag("low_rr", f"R:R {rr1:.2f} below minimum {cfg.min_rr:g} after adjustment"))
        grade = {"A": "B", "B": "C", "C": "C"}[grade]  # type: ignore[assignment]
    if any(f.code == "liquidity" for f in flags):
        grade = "C"

    # ---- risk
    mult = {"A": cfg.grade_a_size, "B": cfg.grade_b_size, "C": cfg.grade_c_size}[grade]
    caution = any(f.reduces_size for f in flags)
    risk_target = risk.risk_usd * mult * (cfg.caution_size if caution else 1.0)

    # ---- size & leverage
    loss_per_coin = per_unit_loss(entry, sl, c, dsign)
    qty_risk = risk_target / loss_per_coin
    notional = qty_risk * entry
    liq: float | None = None
    liq_buf: float | None = None
    if spot:
        lev = 1
        if notional > risk.margin_usd:
            notional = risk.margin_usd
            notes.append("Spot size capped at available margin (no leverage on spot)")
    else:
        lev_needed = max(1, math.ceil(notional / risk.margin_usd - 1e-9))
        tier_max = tier_for(notional, meta.margin_tiers, meta.max_leverage)
        lev_cap = max(1, min(cfg.max_leverage, meta.max_leverage, tier_max))
        lev = min(lev_needed, lev_cap)
        if lev_needed > lev_cap:
            notional = risk.margin_usd * lev_cap
            notes.append(f"Leverage capped at {lev_cap}x; size reduced so margin stays ${risk.margin_usd:,.0f}")
        # liquidation buffer: step leverage down until the stop is hit well before liquidation
        required = cfg.min_liq_buffer_atr * atr
        lowered = False
        while True:
            q_try = notional / entry
            liq = liquidation_price(entry, q_try, q_try * entry / lev, a.direction, meta.margin_tiers, meta.max_leverage)
            liq_buf = (sl - liq) * dsign / atr
            if (sl - liq) * dsign >= required or lev == 1:
                break
            lev -= 1
            lowered = True
            notional = min(notional, risk.margin_usd * lev)
        if lowered:
            flags.append(Flag("liq_buffer", f"leverage lowered to {lev}x so liquidation sits ≥{cfg.min_liq_buffer_atr:g} "
                                            "ATR beyond the stop", reduces_size=False))
    qty = floor_size(notional / entry, sd)
    notional = qty * entry
    margin_used = notional / lev if lev else notional
    if not spot and qty > 0:
        liq = liquidation_price(entry, qty, margin_used, a.direction, meta.margin_tiers, meta.max_leverage)
        liq_buf = (sl - liq) * dsign / atr
        if liq < entry * 1e-6:
            liq = 0.0  # a 1x long cannot be liquidated before price reaches zero
    if notional < HL_MIN_NOTIONAL_USD:
        flags.append(Flag("min_size", f"size ${notional:,.2f} is below Hyperliquid's ${HL_MIN_NOTIONAL_USD:.0f} minimum; "
                                      "raise margin or risk", reduces_size=False))
    risk_usd = qty * loss_per_coin
    half = qty / 2
    usd_tp1 = half * per_unit_gain(entry, tp1, c, dsign)
    usd_tp2_total = usd_tp1 + half * per_unit_gain(entry, tp2, c, dsign)
    fees = notional * c.entry_fee_rate + qty * sl * c.exit_fee_rate  # entry + exit at stop (worst case)
    funding_usd = max(0.0, dsign * c.funding_rate_per_hour * c.hold_hours) * notional

    # ---- win probability / EV
    be = 1 / (1 + rr1) if rr1 > 0 else 1.0
    ev: float | None = None
    basis = "no backtest statistics yet"
    st = gate.stats
    if st is not None:
        if st.expectancy_r is not None and not st.low_sample:
            ev = st.expectancy_r * risk_usd
            basis = f"OOS expectancy {st.expectancy_r:+.2f}R × risk"
        else:
            ev = st.p * (qty * per_unit_gain(entry, tp1, c, dsign)) - (1 - st.p) * risk_usd
            basis = "~ estimate from win probability (low sample)"

    ttl = cfg.plan_ttl_min
    created = int(a.asof.value // 1_000_000)
    expires = created + int(timedelta(minutes=ttl).total_seconds() * 1000)
    runaway = entry + dsign * r  # unfilled limit is cancelled if price runs 1R away
    tf_label = a.trading_tf.upper()
    cmp_word = "below" if dsign > 0 else "above"
    invalid = f"{tf_label} close {cmp_word} {fmt_px(sl_ref)} ({sl_desc})"
    if entry_type == "limit":
        invalid += f" · unfilled after {ttl} min or price beyond {fmt_px(runaway)}"
        entry_note = (f"Prefer limit {fmt_px(zlo_r)}–{fmt_px(zhi_r)} ({zone_label}); use market {fmt_px(price)} only if "
                      "price is already holding inside the zone")
    else:
        entry_note = (f"Market {fmt_px(price)} (setup triggered on close); limit retest {fmt_px(zlo_r)}–{fmt_px(zhi_r)} "
                      f"if price pulls back first")

    return Plan(
        coin=a.coin, direction=a.direction, grade=grade, setup=a.setup.name,
        setup_label=SETUP_LABELS.get(a.setup.name, a.setup.name), confidence=a.confidence,
        expected_move=a.expected_move, spot_only=spot, entry_type=entry_type, entry_market=price,
        entry_zone=(zlo_r, zhi_r), entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp_split=(0.5, 0.5), r_gross=abs(entry - sl),
        rr_tp1=rr1, rr_tp2=rr2, risk_usd_target=risk_target, risk_usd=risk_usd, notional=notional, qty=qty,
        leverage=lev, margin_used=margin_used, liq_price=liq, liq_buffer_atr=liq_buf, fees_usd=fees,
        funding_usd=funding_usd, usd_tp1=usd_tp1, usd_tp2_total=usd_tp2_total, flags=flags, invalidation=invalid,
        entry_note=entry_note, ttl_min=ttl, created_at_ms=created, expires_at_ms=expires, runaway_price=runaway,
        win=st, break_even=be, ev_usd=ev, ev_basis=basis, proof=gate.proof, live_proof=gate.live_proof, notes=notes,
    )
