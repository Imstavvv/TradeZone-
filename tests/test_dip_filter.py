"""Mean-reversion dip buys can wait for the turn and/or the trend (off by default)."""

from tradezone.config import Settings
from tradezone.core.models import Snapshot
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def quant(**kw):
    s = Settings(feed="sim", symbols=["SOL-USD"], starting_cash=1_000.0, **kw)
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:")).bots["quant"]


def snap(price, rsi, sma50=100.0):
    return Snapshot("SOL-USD", price, {"rsi": rsi, "sma50": sma50})


def test_off_by_default_never_waits():
    q = quant()
    assert q.settings.mr_buy_filter == "off" and q.settings.mr_ranging_weight == 0.7
    assert q._dip_wait(snap(95, 25), snap(96, 28)) is None


def test_turn_waits_while_still_falling_and_goes_once_it_ticks_up():
    q = quant(mr_buy_filter="turn")
    assert "still falling" in q._dip_wait(snap(95, 25), snap(96, 28))
    assert "still falling" in q._dip_wait(snap(95, 25), None)
    assert q._dip_wait(snap(96, 29), snap(95, 25)) is None


def test_trend_only_buys_dips_above_the_50_bar_average():
    q = quant(mr_buy_filter="trend")
    assert "downtrend" in q._dip_wait(snap(95, 25, sma50=100), snap(96, 28))
    assert q._dip_wait(snap(105, 25, sma50=100), snap(106, 28)) is None


def test_both_needs_the_trend_and_the_turn():
    q = quant(mr_buy_filter="both")
    assert q._dip_wait(snap(105, 25, sma50=100), snap(106, 28)) is not None  # above, but still falling
    assert q._dip_wait(snap(106, 29, sma50=100), snap(105, 25)) is None
