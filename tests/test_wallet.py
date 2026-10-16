"""Watch-only wallet: trade reconstruction from fills, exit labels, R, position parsing and advice."""

import pytest

from perpdesk.wallet.advice import Position, advise, parse_positions
from perpdesk.wallet.trades import classify_exit, is_perp_coin, r_multiple, rebuild


def fill(coin, side, px, sz, start, t, tid, pnl=0.0, fee=0.0, **kw):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(sz), "startPosition": str(start), "time": t, "tid": tid,
            "closedPnl": str(pnl), "fee": str(fee), **kw}


def test_round_trip_with_scale_in_and_partial_exits():
    fs = [fill("SOL", "B", 100, 1, 0, 1, 1, fee=0.05), fill("SOL", "B", 90, 1, 1, 2, 2, fee=0.05),
          fill("SOL", "A", 110, 1, 2, 3, 3, pnl=15, fee=0.05), fill("SOL", "A", 120, 1, 1, 4, 4, pnl=25, fee=0.05,
                                                                        builderFee="0.02")]
    (t,) = rebuild(list(reversed(fs)))
    assert t.direction == "LONG" and not t.is_open and t.closed_ms == 4
    assert t.entry_px == pytest.approx(95) and t.exit_px == pytest.approx(115) and t.max_size == 2
    assert t.realized_pnl == 40 and t.fees == pytest.approx(0.22) and t.builder_fees == pytest.approx(0.02)
    assert t.net_pnl == pytest.approx(39.78) and t.n_fills == 4


def test_flip_splits_trades_and_fee():
    fs = [fill("ETH", "A", 2000, 1, 0, 1, 1, fee=1.0),
          fill("ETH", "B", 1900, 3, -1, 2, 2, pnl=100, fee=3.0),  # closes 1 short, opens 2 long
          fill("ETH", "A", 1950, 2, 2, 3, 3, pnl=100, fee=2.0)]
    a, b = rebuild(fs)
    assert a.direction == "SHORT" and a.closed_ms == 2 and a.realized_pnl == 100 and a.fees == pytest.approx(2.0)
    assert b.direction == "LONG" and b.opened_ms == 2 and b.open_tid == 2 and b.entry_px == 1900
    assert b.fees == pytest.approx(4.0) and b.realized_pnl == 100 and b.exit_px == 1950 and not b.is_open


def test_partial_history_open_position_and_funding_and_spot_ignored():
    fs = [fill("BTC", "A", 80000, 0.1, 0.3, 10, 7, pnl=50), fill("DOGE", "A", 0.2, 1000, 0, 20, 8),
          fill("@107", "B", 1, 5, 0, 5, 9), fill("PURR/USDC", "B", 1, 5, 0, 5, 10)]
    funding = [{"time": 30, "delta": {"coin": "DOGE", "usdc": "-0.3"}}, {"time": 5, "delta": {"coin": "DOGE", "usdc": "9"}}]
    btc, doge = rebuild(fs, funding)
    assert btc.partial and btc.is_open and btc.entry_px is None and btc.max_size == pytest.approx(0.3)
    assert btc.exit_qty == pytest.approx(0.1) and btc.realized_pnl == 50
    assert doge.direction == "SHORT" and doge.is_open and doge.funding == pytest.approx(-0.3)
    assert not is_perp_coin("@1") and not is_perp_coin("PURR/USDC") and is_perp_coin("xyz:TSLA")


def test_liquidation_flag():
    (t,) = rebuild([fill("HYPE", "B", 40, 1, 0, 1, 1), fill("HYPE", "A", 36, 1, 1, 2, 2, pnl=-4, liquidation={"x": 1})])
    assert t.liquidated and classify_exit("LONG", 40, 36, 37, 45, t.liquidated) == "liquidated"


def test_classify_exit_and_r():
    assert classify_exit("LONG", 100, 95.1, 95, 110, False) == "stop"
    assert classify_exit("LONG", 100, 110, 95, 110, False) == "target"
    assert classify_exit("SHORT", 100, 90, 105, 90, False) == "target"
    assert classify_exit("SHORT", 100, 97, 105, 90, False) == "manual"
    assert classify_exit("SHORT", None, 97, 105, 90, False) == "closed"
    assert r_multiple("LONG", 100, 95, 2, 20) == pytest.approx(2.0)
    assert r_multiple("SHORT", 100, 105, 1, -5) == pytest.approx(-1.0)
    assert r_multiple("LONG", 100, 105, 1, 5) is None and r_multiple("LONG", 100, None, 1, 5) is None


STATE = {"assetPositions": [
    {"position": {"coin": "AERO", "szi": "100", "entryPx": "1.0", "positionValue": "105", "unrealizedPnl": "5",
                  "returnOnEquity": "0.15", "liquidationPx": "0.7", "marginUsed": "33", "leverage": {"type": "isolated",
                                                                                                    "value": 3},
                  "cumFunding": {"sinceOpen": "0.2"}}},
    {"position": {"coin": "ZERO", "szi": "0"}}]}
ORDERS = [
    {"coin": "AERO", "side": "A", "isTrigger": True, "triggerPx": "0.904", "orderType": "Stop Market", "reduceOnly": True},
    {"coin": "AERO", "side": "A", "isTrigger": True, "triggerPx": "1.022", "orderType": "Take Profit Market"},
    {"coin": "AERO", "side": "A", "isTrigger": False, "limitPx": "1.3", "orderType": "Limit", "reduceOnly": True},
    {"coin": "AERO", "side": "B", "isTrigger": False, "limitPx": "0.9", "orderType": "Limit"},  # opening side: ignored
    {"coin": "DOGE", "side": "A", "isTrigger": True, "triggerPx": "1", "orderType": "Stop Market"},
]


def test_parse_positions():
    (p,) = parse_positions(STATE, ORDERS)
    assert p.direction == "LONG" and p.size == 100 and p.mark == pytest.approx(1.05) and p.leverage == 3
    assert p.stops == [0.904] and sorted(p.targets) == [1.022, 1.3] and p.stop == 0.904 and p.target == 1.022
    assert p.funding_since_open == pytest.approx(-0.2) and p.as_dict()["notional"] == pytest.approx(105)


def _view(direction="LONG", sl=0.95, tp1=1.1, tp2=1.2, lev=2):
    return {"direction": direction, "grade": "C", "confidence": 40,
            "plan": {"sl": sl, "tp1": tp1, "tp2": tp2, "leverage": lev}}


def levels(adv):
    return [a.level for a in adv]


def test_advice_flags_tiny_target_vs_wide_stop_and_risk():
    (p,) = parse_positions(STATE, ORDERS)
    adv = advise(p, _view(), 8)
    txt = " ".join(a.text for a in adv)
    assert "R:R 0.23" in txt and "81%" in txt and "TP1 1.1" in txt
    assert "29% of this position's margin" in txt


def test_advice_no_stop_against_signal_liq_and_tp1_passed():
    p = Position("DOGE", "SHORT", 1000, 0.2, 0.19, 0.22, 10, "cross", 20, 10, 0.5, None)
    adv = advise(p, _view("LONG"), 8)
    assert levels(adv)[:2] == ["warn", "danger"] and "against the current signal" in adv[0].text
    assert any("cross margin" in a.text for a in adv)
    p.stops = [0.23]
    assert any(a.level == "danger" and "Liquidation" in a.text for a in advise(p, None, 8))
    p.stops = [0.215]
    assert any("close behind your stop" in a.text for a in advise(p, None, 8))
    q = Position("SOL", "LONG", 1, 100, 112, 50, 20, "isolated", 5, 12, 2, None, stops=[95])
    adv = advise(q, _view(sl=95, tp1=110, tp2=120, lev=5), 100)
    t = " ".join(a.text for a in adv)
    assert "past the plan's TP1" in t and "No take-profit" in t and "20x" in t
    good = Position("SOL", "LONG", 1, 100, 101, 50, 2, "isolated", 50, 1, 0.02, None, stops=[95], targets=[110])
    assert levels(advise(good, _view(sl=95, tp1=110, tp2=120, lev=2), 100)) == ["ok"]
    assert levels(advise(good, {"error": "x"}, 100)) == ["ok"]
