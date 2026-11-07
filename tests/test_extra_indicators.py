"""The wider indicator set reads trends and extremes the way the textbooks do."""

import pytest

from tradezone.bots import quant
from tradezone.core.extra_indicators import ExtraIndicators
from tradezone.core.models import Snapshot


def run(prices, vol=10.0):
    x = ExtraIndicators()
    out = {}
    for p in prices:
        out = x.update(p * 1.001, p * 0.999, p, vol)
    return out


def rising(n=60):
    return [100 * 1.003 ** i for i in range(n)]


def falling(n=60):
    return [100 * 0.997 ** i for i in range(n)]


def test_a_steady_climb_reads_up_on_every_trend_indicator():
    o = run(rising())
    assert o["adx"] > 20 and o["pdi"] > o["mdi"]
    assert o["aroon_up"] == 100 and o["aroon_down"] < 50
    assert o["psar_up"] is True and o["psar"] < rising()[-1]
    assert o["supertrend_up"] is True
    assert o["tenkan"] > o["kijun"] and o["trix"] > 0
    assert o["cmf"] > -1 and o["mfi"] > 50


def test_a_steady_fall_reads_down():
    o = run(falling())
    assert o["pdi"] < o["mdi"] and o["aroon_down"] == 100
    assert o["psar_up"] is False and o["supertrend_up"] is False
    assert o["tenkan"] < o["kijun"] and o["trix"] < 0


def test_the_bottom_of_a_range_reads_oversold():
    prices = [100 + (i % 10) for i in range(50)] + [99 - 0.8 * i for i in range(14)]  # a range, then a 14-bar slide below it
    o = run(prices)
    assert o["williams_r"] < -80 and o["cci"] < -100 and o["mfi"] < 50
    assert o["keltner_lower"] < o["keltner_upper"]


def test_without_volume_the_volume_indicators_stay_out():
    o = run(rising(), vol=0.0)
    assert o["cmf"] is None and o["mfi"] is None and o["adx"] is not None


def test_too_little_history_gives_nothing_yet():
    assert run(rising(10)) == {}


@pytest.mark.parametrize("name,fam", [("adx", "momentum"), ("aroon", "momentum"), ("psar", "momentum"),
                                      ("supertrend", "momentum"), ("ichimoku", "momentum"), ("trix", "momentum"),
                                      ("cmf", "momentum"), ("cci", "mean_reversion"), ("williams", "mean_reversion"),
                                      ("mfi", "mean_reversion"), ("keltner", "breakout")])
def test_each_score_is_between_minus_1_and_1_and_points_the_right_way(name, fam):
    up, down = run(rising()), run(falling())
    vol = {"volatility": 0.003, "atr": 0.3}
    su = quant.STRATEGIES[name](Snapshot("X", rising()[-1], {**up, **vol}))
    sd = quant.STRATEGIES[name](Snapshot("X", falling()[-1], {**down, **vol}))
    assert name in quant.FAMILIES[fam]
    for s in (su, sd):
        assert s is None or -1.0 <= s <= 1.0
    if fam == "momentum" and name != "cmf":
        assert su > 0 > sd  # trend-followers: buy the climb, not the fall
    if fam == "mean_reversion":
        assert (su or 0) <= 0 <= (sd or 0) or su == sd == 0  # dip-buyers never say "buy" at the top of a climb
