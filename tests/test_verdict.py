from datetime import UTC, datetime
from types import SimpleNamespace

from perpdesk.config import Settings
from perpdesk.verdict import Lvl, levels_from_view, next_levels, room_check, verdict

NOW = int(datetime(2026, 10, 19, 12, tzinfo=UTC).timestamp() * 1000)
MIN = 60_000


def _view(**kw):
    v = {"price": 100.0, "updated_at": datetime.fromtimestamp((NOW - 5 * MIN) / 1000, UTC).isoformat(),
         "direction": "SHORT", "grade": "C", "flags": [], "take": False, "take_reason": "",
         "plan": {"entry": 100.0, "sl": 102.0, "tp1": 97.0},
         "levels": [[110.0, "resistance", "resistance ×3", 3], [90.0, "support", "support ×2", 2],
                    [99.5, "support", "support ×1", 1]]}
    v.update(kw)
    return v


def _bot(**kw):
    t = dict(setup="breakout", direction="LONG", fill_px=None, entry=100.0, stop_now=None, sl=98.0, tp1=104.0,
             tp2=108.0, exit_model="fixed", status="pending", created_ms=NOW - 10 * MIN, note="", tp1_hit=False)
    t.update(kw)
    return SimpleNamespace(**t)


S = Settings()


def test_no_bot_is_skip_even_for_good_grade():
    vd = verdict(_view(grade="B"), None, NOW, S)
    assert not vd.take and "no proven-bot signal" in vd.reason


def test_fresh_bot_near_entry_is_take():
    vd = verdict(_view(), _bot(), NOW, S, {"breakout": {"label": "Breakout"}})
    assert vd.take and vd.source == "bot" and vd.bot["direction"] == "LONG"
    assert vd.take_until_ms == NOW - 10 * MIN + 30 * MIN


def test_old_bot_signal_is_skip():
    vd = verdict(_view(), _bot(created_ms=NOW - 45 * MIN), NOW, S)
    assert not vd.take and "45 min old" in vd.reason


def test_drift_and_stop_and_tp1():
    assert "moved +0.5R" in verdict(_view(price=101.0), _bot(), NOW, S).reason
    assert "past the bot's stop" in verdict(_view(price=97.0), _bot(), NOW, S).reason
    assert "TP1" in verdict(_view(), _bot(status="open", tp1_hit=True), NOW, S).reason


def test_market_flag_blocks_but_composite_only_flag_does_not():
    ev = _view(flags=[{"code": "event", "reason": "CPI at 12:30"}])
    vd = verdict(ev, _bot(), NOW, S)
    assert not vd.take and "CPI" in vd.reason
    low = _view(flags=[{"code": "low_rr", "reason": "R:R 0.8"}])
    assert verdict(low, _bot(), NOW, S).take
    # btc_counter on a SHORT composite does not apply to a LONG bot signal
    assert verdict(_view(flags=[{"code": "btc_counter", "reason": "against BTC"}]), _bot(), NOW, S).take
    assert not verdict(_view(direction="LONG", flags=[{"code": "btc_counter", "reason": "x"}]), _bot(), NOW, S).take


def test_stale_card_is_skip():
    old = datetime.fromtimestamp((NOW - 120 * MIN) / 1000, UTC).isoformat()
    vd = verdict(_view(updated_at=old), _bot(), NOW, S)
    assert not vd.take and "stale" in vd.reason


def test_carry_take_when_fresh():
    vd = verdict(_view(), _bot(setup="funding_carry", direction="CARRY", exit_model="carry", sl=0.0, tp1=0.0), NOW, S)
    assert vd.take


def test_composite_take_needs_hint_and_no_flags():
    assert verdict(_view(take=True, take_reason="proven"), None, NOW, S).take
    assert not verdict(_view(take=True, flags=[{"code": "low_rr", "reason": "x"}]), None, NOW, S).take


def test_levels_old_format_and_next_levels():
    lv = levels_from_view({"levels": [[101.0, "resistance", "resistance ×2 (4H)"], [95.0, "pdl", "prior day low"],
                                      [99.0, "support", "support ×1"]]})
    assert [x.strength for x in lv] == [3, 2, 1]
    sup, res = next_levels(lv, 100.0)
    assert sup.price == 95.0 and res.price == 101.0  # ×1 support is not significant


def test_room_check():
    lv = [Lvl(99.5, "support", "support ×3", 3), Lvl(110.0, "resistance", "resistance ×2", 2)]
    assert room_check("SHORT", 100.0, 102.0, 96.0, lv).status == "block"  # support 0.25R below entry
    assert room_check("LONG", 100.0, 98.0, 112.0, lv).status == "caution"  # resistance before TP1
    assert room_check("LONG", 100.0, 98.0, 104.0, lv).status == "ok"
    assert room_check("CARRY", 100.0, 0.0, None, lv).status == "n/a"


def test_room_filter_setting():
    v = _view(levels=[[100.5, "resistance", "resistance ×3", 3]])
    assert verdict(v, _bot(), NOW, S).take  # informational by default
    vd = verdict(v, _bot(), NOW, Settings(room_check_filter=True))
    assert not vd.take and "no room" in vd.reason
