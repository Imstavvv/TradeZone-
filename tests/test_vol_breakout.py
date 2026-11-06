"""Volume breakout: a new 4-hour high on clearly more volume than usual (off by default)."""

from tests.helpers import capture, run
from tradezone.config import Settings
from tradezone.core.models import Snapshot
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(**kw):
    s = Settings(feed="sim", symbols=["SOL-USD", "BTC-USD"], starting_cash=1_000.0, **kw)
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def bar(d, price, volume, historical=False):
    ev = type("E", (), {"payload": Snapshot("SOL-USD", price, {"volume": volume, "rsi": 50.0, "ema_fast": price,
                                                                  "ema_slow": price, "volatility": 0.001, "atr": price * 0.001,
                                                                  "donchian_high": price * 2, "donchian_low": price / 2},
                                            historical=historical)})()
    d.bots["quant"].on_snapshot(ev)
    run(d.bus.drain())


def warm(d, n=250):
    for i in range(n):  # a quiet range: 100-101, volume 10
        bar(d, 100 + (i % 2), 10.0, historical=True)



def test_off_by_default():
    d = make()
    assert d.settings.vol_breakout is False
    warm(d)
    got = capture(d, "signal.proposed")
    bar(d, 105, 30.0)
    assert not any("volume breakout" in s.reason for s in got)


def test_a_new_4_hour_high_on_heavy_volume_is_a_buy_idea():
    d = make(vol_breakout=True)
    warm(d)
    got = capture(d, "signal.proposed")
    bar(d, 105, 30.0)  # above every close of the last 4 hours, 3x the usual volume
    vb = [s for s in got if "volume breakout" in s.reason]
    assert vb and vb[0].side.value == "buy" and vb[0].strength >= 0.55


def test_not_on_quiet_volume_or_inside_the_range():
    d = make(vol_breakout=True)
    warm(d)
    got = capture(d, "signal.proposed")
    bar(d, 105, 11.0)  # a new high, but ordinary volume
    bar(d, 100.5, 40.0)  # heavy volume, but no new high
    assert not any("volume breakout" in s.reason for s in got)


def test_needs_the_full_history_first():
    d = make(vol_breakout=True)
    warm(d, 100)  # under 4 hours of bars
    got = capture(d, "signal.proposed")
    bar(d, 105, 30.0)
    assert not any("volume breakout" in s.reason for s in got)
