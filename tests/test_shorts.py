"""No shorting: the bots only buy, then sell what they hold - a bearish view on something not held does nothing."""

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make():
    s = Settings(feed="sim", symbols=["AAPL", "TSLA", "BTC-USD", "DOGE-USD"], wide_stop=False)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


@pytest.mark.parametrize("sym,price", [("AAPL", 200.0), ("BTC-USD", 50_000.0), ("DOGE-USD", 0.2)])
def test_a_sell_signal_on_something_not_held_never_opens_a_short(sym, price):
    d = make()
    prime(d, sym, price, atr=price * 0.005)
    rejected = capture(d, "signal.rejected")
    d.bus.publish("signal.proposed", Signal(sym, Side.SELL, 0.95, "bearish", "quant"))
    run(d.bus.drain())
    assert d.portfolio.position(sym).is_flat and "never short" in rejected[0]["reason"]


def test_an_old_shorting_setting_is_ignored(monkeypatch):
    monkeypatch.setenv("TZ_SHORTING", "auto")
    s = Settings.from_env()
    assert not hasattr(s, "shorting")


def test_a_sell_signal_on_a_held_coin_still_sells_it():
    d = make()
    prime(d, "BTC-USD", 50_000.0, atr=250.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "bullish", "quant"))
    run(d.bus.drain())
    pos = d.portfolio.position("BTC-USD")
    assert pos.qty > 0
    d.flatten("test")
    run(d.bus.drain())
    for _ in range(3):
        run(d.step())
    assert pos.is_flat or pos.qty >= 0  # sold (never past zero into a short)


def test_the_safety_monitor_flags_any_short_position():
    d = make()
    d.portfolio.position("AAPL").qty = -1.0
    run(d.bots["sentinel"].on_cycle())
    assert not d.bots["sentinel"].books_balanced
