"""What was losing trades (asked for): buys while the timeframes lean down, with weak momentum, in trendless
markets, and skims. The fixes are on (replayed together: +$263.26 training / +$110.14 unseen)."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_new_bots import make, snap
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, SignalKind

pytestmark = pytest.mark.entry_filters

GOOD = {"rsi": 72, "adx": 28, "mtf_ready": 3, "mtf_score": 0.6, "mtf_up": 3, "mtf_down": 0}


def buy(desk, **ind):
    prime(desk, "ETH-USD", 3000.0, atr=12.0)
    snap(desk, "ETH-USD", 3000.0, atr=12.0, **{**GOOD, **ind})
    rej = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position("ETH-USD").qty, [r["reason"] for r in rej]


def test_the_fixes_are_on_by_default():
    s = Settings()
    assert (s.mtf_filter, s.entry_min_rsi, s.entry_min_adx, s.max_skims) == ("weak", 60.0, 15.0, 0)


def test_a_strong_buy_goes_ahead():
    qty, why = buy(make())
    assert qty > 0, why


@pytest.mark.parametrize("ind,said", [
    ({"mtf_score": -0.5, "mtf_up": 1, "mtf_down": 3}, "timeframes lean down"),
    ({"rsi": 52}, "momentum too weak"),
    ({"adx": 11}, "no trend to ride"),
])
def test_the_losing_kinds_of_buy_wait(ind, said):
    qty, why = buy(make(), **ind)
    assert qty == 0 and any(said in w for w in why)


def test_sells_are_never_held_back_by_them():
    desk = make()
    qty, _ = buy(desk)
    snap(desk, "ETH-USD", 3000.0, atr=12.0, rsi=30, adx=5, mtf_ready=3, mtf_score=-1.0)
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.SELL, 1.0, "close", "operator", SignalKind.EXIT, force=True))
    run(desk.bus.drain())
    assert qty > 0 and desk.portfolio.position("ETH-USD").qty == 0
