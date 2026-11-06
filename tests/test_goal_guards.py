"""The goal: grow the balance rapidly with minimal losses - the day lock and the losing-day brake."""

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make():
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD"], starting_cash=10_000.0, loser_cap=0.0)
    s.risk.symbol_cooldown_s = 0
    s.risk.max_position_pct = s.risk.max_gross_exposure = 5.0  # let the size come from the risk, not the caps
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def buy(desk, sym="BTC-USD"):
    prime(desk, sym, 50_000.0, atr=250.0)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym), rejected


def test_a_slipping_good_day_keeps_trading_but_smaller():
    normal = buy(make(), "ETH-USD")[0].qty
    desk = make()
    risk = desk.bots["risk"]
    desk.portfolio.cash += 300.0  # +3% today
    risk._track_day_peak()
    desk.portfolio.cash -= 100.0  # gave back a third: full size
    assert not risk.day_guard()["locked"] and buy(desk)[0].qty > 0
    desk.portfolio.cash -= 80.0  # now over half of the +$300 given back
    g = risk.day_guard()
    assert g["locked"] and g["best_today_pct"] == pytest.approx(0.03)
    pos, _ = buy(desk, "ETH-USD")
    assert 0 < pos.qty < normal * 0.6  # no cap on the day: still trading, at half size
    assert desk.state()["goal"]["guard"]["locked"]


def test_the_old_stop_for_the_day_can_be_switched_back_on():
    desk = make()
    desk.settings.day_lock_stops = True
    risk = desk.bots["risk"]
    desk.portfolio.cash += 300.0
    risk._track_day_peak()
    desk.portfolio.cash -= 180.0
    pos, rejected = buy(desk, "ETH-USD")
    assert pos.is_flat and "locking in today's gains" in rejected[-1]["reason"]


def test_small_gains_never_trigger_the_lock():
    desk = make()
    desk.portfolio.cash += 100.0  # +1%: below the 2% trigger
    desk.bots["risk"]._track_day_peak()
    desk.portfolio.cash -= 90.0
    assert not desk.bots["risk"].day_guard()["locked"]


def test_a_losing_day_halves_new_trades():
    normal = buy(make())[0].qty
    desk = make()
    desk.portfolio.cash -= 300.0  # -3%: past half of the 5% practice daily limit
    assert desk.bots["risk"].day_guard()["brake"]
    braked = buy(desk)[0].qty
    assert 0 < braked < normal * 0.6
