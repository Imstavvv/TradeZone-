"""Accumulating: the Flipper sells a slice of a held coin into a rise and buys it back cheaper - more coins."""

import pytest

from tests.helpers import prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(**kw):
    s = Settings(feed="sim", symbols=["DOGE-USD", "BTC-USD"], skimmer=False, **{"wide_stop": True, "accumulate": True, **kw})
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def hold(desk, sym="DOGE-USD", price=0.2):
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def test_sell_a_slice_into_a_rise_and_buy_it_back_cheaper_for_more_coins():
    desk = make()
    f = desk.bots["flipper"]
    pos = hold(desk)
    q0, entry = pos.qty, pos.avg_price
    tick(desk, "DOGE-USD", entry * 1.035)  # +3.5%: sell 20% into the rise
    assert pos.qty == pytest.approx(q0 * 0.8, rel=0.01) and "DOGE-USD" in f.cycles
    f._acc_next.clear()
    tick(desk, "DOGE-USD", entry * 1.02)  # only 1.5% below the sale: not yet
    assert pos.qty == pytest.approx(q0 * 0.8, rel=0.01)
    f._acc_next.clear()
    tick(desk, "DOGE-USD", entry * 1.0)  # ~3.4% below the sale: buy it back
    assert pos.qty > q0  # more coins than at the start
    assert f.coins_gained["DOGE-USD"] > 0 and f.metrics["acc_rounds"] == 1 and "DOGE-USD" not in f.cycles
    assert desk.bots["sentinel"].audit_books() == []


@pytest.mark.with_killed  # (tests a feature switched off after failing on unseen data)
def test_never_buys_back_higher_the_slice_stays_as_dollar_profit():
    desk = make()
    f = desk.bots["flipper"]
    pos = hold(desk)
    entry = pos.avg_price
    tick(desk, "DOGE-USD", entry * 1.04)
    q = pos.qty
    f._acc_next.clear()
    tick(desk, "DOGE-USD", entry * 1.10)  # it kept rising: no buy-back
    assert pos.qty == pytest.approx(q)
    f.cycles["DOGE-USD"]["ts"] -= 49 * 3600  # two days, no dip
    f._acc_next.clear()
    tick(desk, "DOGE-USD", entry * 1.10)
    assert "DOGE-USD" not in f.cycles and f.metrics["acc_kept_cash"] == 1


def test_no_slice_is_sold_at_a_loss_or_on_a_small_rise():
    desk = make()
    pos = hold(desk)
    q0 = pos.qty
    tick(desk, "DOGE-USD", pos.avg_price * 1.01)  # +1%: not enough
    assert pos.qty == pytest.approx(q0) and not desk.bots["flipper"].cycles


def test_it_can_be_switched_off():
    desk = make(accumulate=False)
    pos = hold(desk)
    q0 = pos.qty
    tick(desk, "DOGE-USD", pos.avg_price * 1.04)
    assert pos.qty == pytest.approx(q0)


def test_what_it_holds_out_and_gained_survives_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    d1 = TradingDesk(Settings(feed="sim", symbols=["DOGE-USD"]), journal=Journal(db))
    d1.bots["flipper"].cycles["DOGE-USD"] = {"qty": 10.0, "cash": 2.1, "px": 0.21, "ts": 1.0}
    d1.bots["flipper"].coins_gained["DOGE-USD"] = 3.5
    d1.bots["flipper"].save()
    d2 = TradingDesk(Settings(feed="sim", symbols=["DOGE-USD"]), journal=Journal(db))
    assert d2.bots["flipper"].load() and d2.bots["flipper"].coins_gained["DOGE-USD"] == 3.5
    assert d2.state()["flipper"]["slices_out"]["DOGE-USD"]["qty"] == 10.0


def test_accumulating_is_off_unless_turned_on():
    """It capped winners on real price history, so it's opt-in (TZ_ACCUMULATE=true)."""
    assert Settings().accumulate is False and Settings().flip_bank_fraction == 0.0
