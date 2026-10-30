"""Portfolio study rules: caps, same-direction limit, coin overlap, regime filters, cool-off and the pass rule."""

from perpdesk.lab.portfolio import Rules, T, metrics, passes, replay

H = 3_600_000
DAY = 24 * H


def tr(bot, coin, d, t_h, end_h, r, adx=30.0, btc=30.0):
    return T(bot, coin, d, int(t_h * H), int(end_h * H), r, adx, btc)


def test_live_caps_and_one_per_coin_per_bot():
    xs = [tr("breakout", c, "LONG", 0, 10, -1) for c in "ABCD"] + [tr("breakout", "A", "LONG", 1, 5, 1)]
    got = replay(xs, Rules())
    assert [x.coin for x in got] == ["A", "B", "C"]  # cap 3, A not twice


def test_same_direction_and_coin_overlap():
    xs = [tr("breakout", "A", "LONG", 0, 10, -1), tr("trend_follow", "A", "LONG", 0, 10, -1),
          tr("trend_follow", "B", "LONG", 1, 10, -1), tr("trend_follow", "C", "SHORT", 2, 10, 1),
          tr("trend_follow", "D", "LONG", 11, 20, 1)]
    assert len(replay(xs, Rules())) == 5
    got = replay(xs, Rules(max_same_dir=1, no_coin_overlap=True))
    assert [(x.coin, x.direction) for x in got] == [("A", "LONG"), ("C", "SHORT"), ("D", "LONG")]
    assert len(replay(xs, Rules(max_open_r=2, no_coin_overlap=True))) == 3


def test_regime_filters_and_cooloff():
    xs = [tr("breakout", "A", "LONG", 0, 1, -1, adx=15), tr("breakout", "B", "LONG", 2, 3, -1, btc=10)]
    assert len(replay(xs, Rules(min_adx=20))) == 1 and len(replay(xs, Rules(min_btc_adx=20))) == 1
    losses = [tr("breakout", c, "LONG", i * 2, i * 2 + 1, -1) for i, c in enumerate("ABC")]
    later = [tr("breakout", "D", "LONG", 10, 12, 2), tr("breakout", "E", "SHORT", 10, 12, 2),
             tr("breakout", "F", "LONG", 60, 62, 2)]
    got = replay(losses + later, Rules(cooloff=(3, 48)))
    assert {x.coin for x in got} == {"A", "B", "C", "E", "F"}  # D paused (within 48h of the 3rd loss), F after


def test_metrics_and_pass_rule():
    base = [tr("breakout", f"C{i}", "LONG", i * DAY / H, i * DAY / H + 1, r) for i, r in enumerate([1, -1, -1, -1, 2, 1])]
    m = metrics(base)
    assert m["n"] == 6 and m["sum_r"] == 1.0 and m["max_dd"] == 3.0 and m["lose_streak"] == 3
    better = metrics([x for x in base if x.r > -1 or x.coin == "C1"])
    ok, why = passes(m, better)
    assert better["max_dd"] < m["max_dd"] and not ok and any("years" in w for w in why)  # one year only: fails rule 3
