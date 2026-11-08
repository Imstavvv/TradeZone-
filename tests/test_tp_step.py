"""Moving take-profit: once a trade is 10% past its buy price, the target moves out and the stop locks profit."""

import pytest

from tests.helpers import capture, prime, run
from tradezone.core.models import Side, Signal, Tick


def open_trade(desk, sym, price, side=Side.BUY):
    if sym not in desk.settings.symbols:
        desk.settings.symbols.append(sym)
    desk.settings.skimmer = False  # the Portfolio Manager on its own
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, side, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


@pytest.mark.old_plan  # (a mechanism test, written around the stops / targets before -7% / +15%)
@pytest.mark.with_killed  # (tests a feature switched off after failing on unseen data)
def test_past_10_percent_the_target_moves_out_instead_of_selling(desk):
    pos = open_trade(desk, "DOGE-USD", 0.2)  # meme coin: target +15% after fees
    q0, entry, old_tp = pos.qty, pos.avg_price, pos.take_profit
    assert old_tp < entry * 1.16
    tick(desk, "DOGE-USD", entry * 1.105)  # +10.5% from the buy price
    assert pos.take_profit > entry * 1.20 and pos.stop_price >= entry * 1.05  # target out, +5% locked
    tick(desk, "DOGE-USD", entry * 1.16)  # past the OLD target: not sold any more
    assert pos.qty == pytest.approx(q0)
    tick(desk, "DOGE-USD", entry * 1.21)  # +21%: the next step
    assert pos.take_profit > entry * 1.30 and pos.stop_price >= entry * 1.10
    assert desk.bots["portfolio"].metrics["tp_steps"] == 2
    trades = capture(desk, "trade.closed")
    tick(desk, "DOGE-USD", entry * 1.09)  # pulls back: the locked stop sells in profit
    assert pos.is_flat and trades[-1].pnl > 0 and trades[-1].exit_price >= entry * 1.085  # (sold on the pullback tick)


@pytest.mark.tp15  # (written around the +15% target)
def test_it_can_be_switched_off(desk):
    desk.settings.tp_step = False
    pos = open_trade(desk, "DOGE-USD", 0.2)
    tp = pos.take_profit
    tick(desk, "DOGE-USD", pos.avg_price * 1.105)
    assert pos.take_profit == tp


@pytest.mark.old_plan  # (a mechanism test, written around the stops / targets before -7% / +15%)
def test_each_new_trade_counts_from_its_own_buy_price(desk):
    pos = open_trade(desk, "DOGE-USD", 0.2)
    tick(desk, "DOGE-USD", pos.avg_price * 1.105)
    assert desk.bots["portfolio"].tp_steps["DOGE-USD"] == 1
    tick(desk, "DOGE-USD", pos.avg_price * 1.0)  # stopped out at the locked +5%
    assert pos.is_flat and "DOGE-USD" not in desk.bots["portfolio"].tp_steps
