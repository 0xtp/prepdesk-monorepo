"""Trade plan maths: sizing, leverage, liquidation, rounding, grading. Includes property tests."""

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helpers import Flag, Level, RangeInfo, make_analysis
from perpdesk.analysis.types import AssetMeta
from perpdesk.plan import (
    GateInfo,
    PlanConfig,
    RiskInputs,
    WinStats,
    build_plan,
    floor_size,
    liquidation_price,
    liquidation_price_simple,
    maintenance_margin,
    net_rr,
    round_price,
)

META = AssetMeta("TEST", True, 2, 20, [], False, "Large")


def test_liquidation_matches_closed_form_single_tier():
    for d in ("LONG", "SHORT"):
        for lev in (1, 2, 5, 10, 20):
            q = 3.0
            liq = liquidation_price(100.0, q, q * 100 / lev, d, [], 20)
            assert liq == pytest.approx(liquidation_price_simple(100.0, lev, d, 20), rel=1e-9, abs=1e-9)


def test_liq_long_below_short_above():
    assert liquidation_price(100, 1, 10, "LONG", [], 20) < 100
    assert liquidation_price(100, 1, 10, "SHORT", [], 20) > 100


def test_maintenance_margin_tiers_continuous():
    tiers = [(0.0, 10), (3_000_000.0, 5)]
    below = maintenance_margin(2_999_999.0, tiers, 10)
    above = maintenance_margin(3_000_001.0, tiers, 10)
    assert above - below == pytest.approx(2 / 20 + 0, abs=1)  # continuous, no jump
    assert maintenance_margin(1000.0, tiers, 10) == pytest.approx(50.0)


@pytest.mark.parametrize("px,sd,expected", [(106.0123, 2, 106.01), (82467.3, 5, 82467.0), (0.00761234, 0, 0.007612),
                                            (1238.85, 2, 1238.8), (12.34567, 1, 12.346)])
def test_round_price_hl_rules(px, sd, expected):
    assert round_price(px, sd) == pytest.approx(expected)


def test_floor_size_decimals():
    assert floor_size(12.239, 2) == 12.23
    assert floor_size(5.9, 0) == 5.0
    assert floor_size(0.123456, 5) == 0.12345


def test_basic_long_plan_numbers():
    lv = [Level(98.5, "support", 2), Level(103.0, "resistance", 2), Level(99.5, "vwap")]
    a = make_analysis(levels=lv, atr=1.0)
    p = build_plan(a, META, RiskInputs(250, 20), PlanConfig())
    assert p.direction == "LONG" and p.sl < p.entry < p.tp1 < p.tp2
    assert p.risk_usd <= p.risk_usd_target + 1e-9
    assert p.leverage <= 10
    assert p.liq_price is not None and p.liq_price < p.sl


def test_tiny_stop_widened_to_min_atr():
    lv = [Level(99.9, "support", 2)]
    a = make_analysis(levels=lv, atr=2.0, setup="breakout", entry_type="market")
    p = build_plan(a, META, RiskInputs(250, 20), PlanConfig(min_sl_atr=0.8))
    assert p.entry - p.sl >= 0.8 * 2.0 - 0.02


def test_max_leverage_cap_shrinks_size():
    a = make_analysis(levels=[Level(99.0, "support", 2)], atr=0.3)
    p = build_plan(a, META, RiskInputs(50, 20), PlanConfig(max_leverage=3, min_liq_buffer_atr=0))
    assert p.leverage <= 3
    assert p.notional <= 50 * 3 + 1e-6
    assert any("capped" in n for n in p.notes)


def test_liq_buffer_lowers_leverage_and_flags():
    a = make_analysis(levels=[Level(99.0, "support", 2)], atr=1.0)
    meta = AssetMeta("T", True, 2, 3, [], False, "Mid")  # 3x max -> maintenance 1/6
    p = build_plan(a, meta, RiskInputs(20, 20), PlanConfig(min_liq_buffer_atr=5))
    assert p.liq_price is not None
    if p.leverage > 1:
        assert (p.sl - p.liq_price) >= 5 * 1.0 - 1e-6
    assert any(f.code == "liq_buffer" for f in p.flags) or (p.sl - p.liq_price) >= 5


def test_low_price_asset_rounding():
    a = make_analysis(price=0.007615, atr=0.0001, levels=[Level(0.0075, "support", 2)])
    meta = AssetMeta("PENGU", True, 0, 5, [], False, "Memes")
    p = build_plan(a, meta, RiskInputs(250, 20), PlanConfig())
    assert p.qty == int(p.qty)
    assert p.sl < p.entry


def test_spot_only_never_levered():
    a = make_analysis(levels=[Level(98, "support", 2)])
    meta = AssetMeta("X", False, 4, 1, [], False, "Mid")
    p = build_plan(a, meta, RiskInputs(100, 20), PlanConfig())
    assert p.spot_only and p.leverage == 1 and p.liq_price is None
    assert p.notional <= 100 + 1e-6
    assert "-PERP" not in p.copy_text()


def test_grades_and_multipliers():
    lv = [Level(98.5, "support", 2), Level(110, "resistance", 2)]
    base = dict(levels=lv, setup="trend_pullback", confidence=80)
    a = make_analysis(**base)
    pa = build_plan(a, META, RiskInputs(250, 20), PlanConfig(), GateInfo(passed=True))
    pb = build_plan(a, META, RiskInputs(250, 20), PlanConfig(), GateInfo(passed=False))
    pc = build_plan(make_analysis(levels=lv, confidence=40), META, RiskInputs(250, 20), PlanConfig())
    assert (pa.grade, pb.grade, pc.grade) == ("A", "B", "C")
    assert pa.risk_usd_target == pytest.approx(20) and pb.risk_usd_target == pytest.approx(10)
    assert pc.risk_usd_target == pytest.approx(5)
    composite_gate = build_plan(make_analysis(levels=lv, confidence=90), META, RiskInputs(250, 20), PlanConfig(),
                                GateInfo(passed=True))
    assert composite_gate.grade != "A"  # composite bias can never be A
    demoted = build_plan(a, META, RiskInputs(250, 20), PlanConfig(), GateInfo(passed=True, demoted_reason="x"))
    assert demoted.grade == "B"


def test_caution_flag_reduces_size_and_liquidity_caps_c():
    lv = [Level(98.5, "support", 2)]
    a = make_analysis(levels=lv, confidence=80, flags=[Flag("event", "CPI soon")])
    p = build_plan(a, META, RiskInputs(250, 20), PlanConfig())
    assert p.risk_usd_target == pytest.approx(20 * 0.5 * 0.5)
    a2 = make_analysis(levels=lv, confidence=95, setup="breakout", entry_type="market",
                       flags=[Flag("liquidity", "thin")])
    assert build_plan(a2, META, RiskInputs(250, 20), PlanConfig(), GateInfo(passed=True)).grade == "C"


def test_low_rr_drops_grade():
    lv = [Level(98.0, "support", 2), Level(100.3, "resistance", 3), Level(100.5, "resistance", 3)]
    a = make_analysis(levels=lv, confidence=80, setup="breakout", entry_type="market")
    p = build_plan(a, META, RiskInputs(250, 20), PlanConfig(min_rr=3.0), GateInfo(passed=True))
    if p.rr_tp1 < 3.0:
        assert p.grade == "B" and any(f.code == "low_rr" for f in p.flags)


def test_range_trade_targets_mid():
    rng = RangeInfo(110.0, 100.0, 3, 3)
    a = make_analysis(price=103.0, atr=1.0, expected="SIDEWAYS", rng=rng)
    p = build_plan(a, META, RiskInputs(250, 20), PlanConfig())
    assert p.tp1 == pytest.approx(105.0, abs=0.01)
    assert p.sl < 100.0


def test_win_stats_ev_and_break_even():
    lv = [Level(98.5, "support", 2)]
    ws = WinStats(0.55, 0.5, 0.6, 200, "x", False, expectancy_r=0.2)
    p = build_plan(make_analysis(levels=lv), META, RiskInputs(250, 20), PlanConfig(), GateInfo(stats=ws))
    assert p.break_even == pytest.approx(1 / (1 + p.rr_tp1))
    assert p.ev_usd == pytest.approx(0.2 * p.risk_usd)


def test_short_mirror():
    lv = [Level(101.5, "resistance", 2), Level(97, "support", 2)]
    p = build_plan(make_analysis("SHORT", levels=lv), META, RiskInputs(250, 20), PlanConfig())
    assert p.tp2 < p.tp1 < p.entry < p.sl
    assert p.liq_price is not None and p.liq_price > p.sl


def test_costs_reduce_rr():
    from perpdesk.plan import Costs

    free = Costs(0, 0, 0, 0, 0)
    paid = Costs(0.00045, 0.00045, 0.0003, 0.0001, 12)
    assert net_rr(100, 99, 102, free, 1) == pytest.approx(2.0)
    assert net_rr(100, 99, 102, paid, 1) < 2.0


# ------------------------------------------------------------------ property tests

levels_st = st.lists(st.floats(min_value=0.5, max_value=1.5), min_size=0, max_size=8)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    direction=st.sampled_from(["LONG", "SHORT"]),
    price=st.floats(min_value=0.0001, max_value=200_000),
    atr_pct=st.floats(min_value=0.0005, max_value=0.08),
    rel_levels=levels_st,
    sz=st.integers(min_value=0, max_value=5),
    maxlev=st.sampled_from([3, 5, 10, 20, 25, 40, 50]),
    cfg_maxlev=st.integers(min_value=1, max_value=50),
    margin=st.floats(min_value=20, max_value=50_000),
    risk_pct=st.floats(min_value=0.5, max_value=10),
    conf=st.integers(min_value=0, max_value=100),
    entry_type=st.sampled_from(["limit", "market"]),
    funding=st.floats(min_value=-0.001, max_value=0.001),
    buffer=st.floats(min_value=0, max_value=6),
)
def test_plan_invariants(direction, price, atr_pct, rel_levels, sz, maxlev, cfg_maxlev, margin, risk_pct, conf,
                         entry_type, funding, buffer):
    atr = price * atr_pct
    levels = [Level(price * (1 + (r - 1) * atr_pct * 4), "support" if r < 1 else "resistance", 2) for r in rel_levels]
    a = make_analysis(direction, price=price, atr=atr, levels=levels, confidence=conf,
                      setup="breakout" if entry_type == "market" else "composite", entry_type=entry_type)
    meta = AssetMeta("P", True, sz, maxlev, [(0.0, maxlev)], False, "Mid")
    risk = RiskInputs(margin, margin * risk_pct / 100)
    cfg = PlanConfig(max_leverage=cfg_maxlev, min_liq_buffer_atr=buffer)
    try:
        p = build_plan(a, meta, risk, cfg, current_funding=funding)
    except ValueError:
        return  # degenerate price rounding (e.g. stop rounds onto entry) is rejected, never mis-sized
    d = 1 if direction == "LONG" else -1
    # stop always on the correct side of entry, targets on the other
    assert (p.entry - p.sl) * d > 0
    assert (p.tp1 - p.entry) * d > 0
    # risk in $ never exceeds the configured risk
    assert p.risk_usd <= risk.risk_usd + 1e-9
    assert p.risk_usd <= p.risk_usd_target + 1e-9
    # size respects size decimals, leverage respects every cap
    assert abs(p.qty * 10**sz - round(p.qty * 10**sz)) < 1e-6
    assert 1 <= p.leverage <= max(1, min(cfg_maxlev, maxlev))
    assert p.margin_used <= margin + 1e-6
    # the stop is hit before the estimated liquidation
    if p.qty > 0 and p.liq_price is not None:
        assert (p.sl - p.liq_price) * d > 0 or (direction == "LONG" and p.liq_price == 0.0)
        if p.leverage > 1:
            assert (p.sl - p.liq_price) * d >= buffer * atr - 1e-6 * price
    assert math.isfinite(p.rr_tp1)


# ---------------------------------------------------------------- leverage slider (what-if)

def _costs(funding=0.0):
    from perpdesk.plan import Costs

    return Costs(0.00015, 0.00045, 0.0003, funding, 12)


def test_leverage_scenario_plan_mode_and_smaller_size():
    from perpdesk.plan import leverage_scenario

    meta = AssetMeta("ZEC", True, 2, 10, [], False, "Mid")
    # entry 100, stop 95 (5 away, ATR 2.5 -> 2 ATR), budget $20, margin $100
    sc = leverage_scenario("LONG", 100, 95, 110, 120, 2.5, 100, 20, 3, meta, _costs(), 0.8, 3.0, plan_notional=300)
    assert sc.feasible and sc.mode == "plan" and sc.sl == 95 and sc.tp1 == 110 and sc.level == "ok"
    assert sc.risk_usd <= 20 and sc.qty == 3
    lo = leverage_scenario("LONG", 100, 95, 110, 120, 2.5, 100, 20, 2, meta, _costs(), 0.8, 3.0)
    assert lo.mode == "plan" and lo.risk_usd < 20 and "smaller size" in lo.messages[0]


def test_leverage_scenario_scales_stop_and_flags_noise_and_keep_stop_loss():
    from perpdesk.plan import leverage_scenario, per_unit_loss

    meta = AssetMeta("ZEC", True, 2, 10, [], False, "Mid")
    sc = leverage_scenario("LONG", 100, 95, 110, 120, 2.5, 100, 20, 10, meta, _costs(), 0.8, 3.0)
    assert sc.feasible and sc.mode == "scaled"
    assert 95 < sc.sl < 100 and 100 < sc.tp1 < 110 and sc.tp1 < sc.tp2
    assert sc.risk_usd == pytest.approx(20, rel=0.05)
    assert sc.qty * per_unit_loss(100, sc.sl, _costs(), 1) <= 20 * 1.02
    assert sc.level == "danger" and "inside normal price noise" in sc.messages[0]
    assert sc.keep_sl_loss == pytest.approx(10 * per_unit_loss(100, 95, _costs(), 1))
    assert any("Keeping the plan's stop" in m and "of your margin" in m for m in sc.messages)
    # moderate over-leverage: stop tightened but still > 0.8 ATR -> warn, not danger
    mid = leverage_scenario("SHORT", 100, 108, 90, 80, 2.5, 100, 20, 4, meta, _costs(), 0.8, 3.0)
    assert mid.mode == "scaled" and mid.level == "warn" and 100 < mid.sl < 108 and mid.tp1 < 100


def test_leverage_scenario_infeasible_cases_and_liq_before_stop():
    from perpdesk.plan import leverage_scenario

    meta = AssetMeta("ZEC", True, 2, 10, [(0.0, 10), (500.0, 5)], False, "Mid")
    tier = leverage_scenario("LONG", 100, 95, 110, 120, 2.5, 100, 20, 8, meta, _costs(), 0.8, 3.0)
    assert not tier.feasible and "at most 5x" in tier.messages[0]
    tiny = leverage_scenario("LONG", 100, 95, 110, 120, 2.5, 3, 1, 2, AssetMeta("Z", True, 2, 10, [], False, "Mid"),
                             _costs(), 0.8, 3.0)
    assert not tiny.feasible and "minimum" in tiny.messages[0]
    fees = leverage_scenario("LONG", 100, 95, 110, 120, 2.5, 100, 0.3, 10, AssetMeta("Z", True, 2, 10, [], False, "Mid"),
                             _costs(), 0.8, 3.0)
    assert not fees.feasible and "fees alone" in fees.messages[0] and "Keeping the plan's stop" in fees.messages[1]
    # wide stop at high leverage with a generous budget: liquidation lands before the plan's stop
    big = AssetMeta("Z", True, 2, 20, [], False, "Mid")
    liq = leverage_scenario("LONG", 100, 80, 130, 150, 5, 100, 10_000, 10, big, _costs(), 0.8, 3.0)
    assert liq.mode == "plan" and liq.level == "danger" and "Liquidation" in liq.messages[0] and liq.keep_sl_liquidated
    near = leverage_scenario("LONG", 100, 94, 110, 120, 5, 100, 10_000, 10, big, _costs(), 0.8, 3.0)
    assert near.level == "warn" and any("ATR beyond the stop" in m for m in near.messages)
