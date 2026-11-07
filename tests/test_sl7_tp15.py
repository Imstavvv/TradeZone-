"""Stop-loss at most -7%, take-profit +15% after fees - new trades and the ones already open (asked for)."""

import pytest

from tests.helpers import capture, run
from tests.test_wide_stop import buy, tick
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.persistence import StateStore


def make(db=":memory:", **kw):
    s = Settings(loser_cap=0.0, feed="sim", symbols=["BTC-USD", "ETH-USD", "DOGE-USD"], **kw)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))


@pytest.mark.parametrize("sym,price", [("BTC-USD", 50_000.0), ("DOGE-USD", 0.2)])
def test_a_new_trade_has_a_7_percent_stop_and_a_15_percent_target_after_fees(sym, price):
    d = make()
    pos = buy(d, sym, price)
    assert pos.qty > 0
    assert pos.stop_price >= pos.avg_price * 0.93 * 0.999  # never more than 7% away
    target = pos.avg_price * 1.15 + d.settings.round_trip_cost(sym, pos.avg_price)
    assert pos.take_profit == pytest.approx(target, rel=0.01)  # +15% after the buy and sell fees


def test_never_sell_on_still_sells_a_trade_7_percent_down():
    d = make()
    assert d.settings.never_sell_at_loss and d.settings.max_loss_pct == 0.07
    pos = buy(d)
    trades = capture(d, "trade.closed")
    tick(d, "BTC-USD", pos.avg_price * 0.95)  # -5%: held
    assert pos.qty > 0
    tick(d, "BTC-USD", pos.avg_price * 0.925)  # -7.5%: past the floor
    for _ in range(3):
        run(d.step())
    assert pos.is_flat and trades and trades[-1].pnl < 0


def test_open_trades_get_the_new_stop_and_target_once_and_a_saved_20_percent_floor_becomes_7(tmp_path):
    db = str(tmp_path / "tz.db")
    d = make(db)
    pos = buy(d)
    pos.stop_price, pos.take_profit = pos.avg_price * 0.80, pos.avg_price * 1.04  # (the old -20% / +4%)
    d.save_state()
    StateStore(d.journal).save("loss_floor", {"pct": 0.20})  # (saved before -7%)
    d2 = make(db)
    alerts = capture(d2, "alert")
    run(d2.start())
    p2 = d2.portfolio.position("BTC-USD")
    assert p2.qty > 0 and p2.stop_price == pytest.approx(p2.avg_price * 0.93)
    assert p2.take_profit == pytest.approx(p2.avg_price * 1.15 + d2.settings.round_trip_cost("BTC-USD", p2.avg_price))
    assert d2.settings.max_loss_pct == 0.07
    assert any("New rules on 1 open trade" in a.message for a in alerts)
    p2.take_profit = p2.avg_price * 1.30  # (changed later on purpose...)
    d2.save_state()
    run(d2.shutdown())
    d3 = make(db)
    run(d3.start())
    assert d3.portfolio.position("BTC-USD").take_profit == pytest.approx(p2.avg_price * 1.30)  # ...kept: once only
    run(d3.shutdown())


def test_at_plus_15_percent_after_fees_a_third_is_sold_before_the_target_moves():
    """The moving take-profit used to move the target out at +15% before fees - so nothing was ever sold at +15%."""
    d = make()
    pos = buy(d)
    start = pos.qty
    trades = capture(d, "trade.log")
    tick(d, "BTC-USD", pos.take_profit * 1.001)
    for _ in range(3):
        run(d.step())
    assert pos.qty == pytest.approx(start * (1 - d.settings.runner_sell_fraction), rel=0.02)  # a third banked...
    assert any(t.get("action") == "partial" for t in trades)
    assert pos.stop_price > pos.avg_price  # ...the rest rides with its stop locked in profit


def test_a_stop_in_profit_always_sits_where_selling_covers_every_cost():
    """Profit-taking always covers all costs (asked for): a trailing stop above the buy price but under the fees is
    lifted, so the sale at it keeps a profit after both fees (never-sell would otherwise just hold the trade)."""
    d = make()
    assert d.settings.fee_rule_stops
    pos = buy(d)
    entry = pos.avg_price
    tick(d, "BTC-USD", entry * 1.04)  # up 4%
    pos.stop_price = entry * 1.001  # (a trailing stop just above the buy price: under the fees)
    tick(d, "BTC-USD", entry * 1.04)
    cost = d.settings.round_trip_cost("BTC-USD", entry)
    assert pos.stop_price >= entry + cost  # lifted to where selling covers the costs (and more)
    trades = capture(d, "trade.closed")
    tick(d, "BTC-USD", pos.stop_price * 0.999)  # falls back through it
    for _ in range(3):
        run(d.step())
    assert trades and all(t.pnl > 0 for t in trades)  # sold, in profit after every cost
