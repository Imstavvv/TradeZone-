"""Cheaper exits: profit-taking sells wait at the ask (maker fee); stops and cuts always go straight to market."""

import pytest

from tests.helpers import capture, run
from tests.test_kraken import check, kbuy, live_kraken
from tradezone.core.models import Side, Signal, SignalKind, Tick


def tick(desk, fk, px):
    fk.price["XBTUSD"] = px
    desk.prices["BTC-USD"] = px
    desk.bus.publish("market.tick", Tick("BTC-USD", px))
    run(desk.bus.drain())


def open_trade():
    fk, desk = live_kraken()
    desk.settings.maker_exits = True  # (opt-in: TZ_MAKER_EXITS=true)
    desk.settings.runners = False  # (a plain take-profit: sell it all)
    desk.settings.scale_out_fraction = 0.0
    pos = kbuy(desk)
    assert pos.qty > 0
    return fk, desk, pos


def test_a_take_profit_sells_at_the_ask_for_the_maker_fee():
    fk, desk, pos = open_trade()
    target = pos.take_profit
    tick(desk, fk, target * 1.001)
    assert pos.is_flat
    assert fk.ordertypes[-1] == "limit" and "post" in fk.oflags[-1]  # waited at the ask, not a market sell


def test_a_stop_always_sells_at_market():
    fk, desk, pos = open_trade()
    pos.stop_price = 59_000.0
    tick(desk, fk, 58_900.0)
    assert pos.is_flat and fk.ordertypes[-1] == "market"


def test_a_stop_never_waits_behind_a_resting_profit_sale():
    fk, desk, pos = open_trade()
    fk.limit_fills = False  # the profit-taking sell rests at the ask, nobody buys
    exe = desk.bots["execution"]
    tick(desk, fk, pos.take_profit * 1.001)
    assert not pos.is_flat and any(o.reduce_only and o.maker for o in exe.working.values())
    pos.stop_price = pos.avg_price * 0.98
    tick(desk, fk, pos.avg_price * 0.97)  # the price turns and falls through the stop
    assert pos.is_flat and fk.ordertypes[-1] == "market"  # cancelled the resting sale, sold at market at once
    assert exe.metrics.get("maker_cancelled_for_urgent") == 1 and not exe.working
    assert fk.bal.get("XXBT", 0) == pytest.approx(0, abs=1e-9)  # sold exactly once - nothing extra
    assert check(desk) == [] and not desk.paused


def test_an_unfilled_profit_sale_is_sold_at_market_after_the_wait():
    fk, desk, pos = open_trade()
    fk.limit_fills = False
    exe = desk.bots["execution"]
    tick(desk, fk, pos.take_profit * 1.001)
    for o in exe.working.values():
        exe._submitted_at[o.id] -= desk.settings.maker_wait_s + 1
    run(exe.on_cycle())
    run(desk.bus.drain())
    assert pos.is_flat and fk.ordertypes[-1] == "market"  # never missed
    assert fk.bal.get("XXBT", 0) == pytest.approx(0, abs=1e-9) and check(desk) == []


def test_a_partly_filled_profit_sale_never_sells_more_than_is_held():
    fk, desk, pos = open_trade()
    fk.limit_fills = False
    exe = desk.bots["execution"]
    tick(desk, fk, pos.take_profit * 1.001)
    resting = next(iter(exe.working.values()))
    o = fk.orders[resting.broker_id]  # Kraken fills 40% at the ask...
    vol = float(resting.qty) * 0.4
    px = fk.price["XBTUSD"]
    fk.bal["XXBT"] -= vol
    fk.bal["ZUSD"] += vol * px - vol * px * 0.0016
    o.update(status="open", vol_exec=f"{vol:.8f}", cost=f"{vol * px:.8f}", fee=f"{vol * px * 0.0016:.8f}")
    exe._submitted_at[resting.id] -= desk.settings.maker_wait_s + 1
    run(exe.on_cycle())  # ...the rest goes at market
    run(desk.bus.drain())
    assert pos.is_flat and fk.bal.get("XXBT", 0) == pytest.approx(0, abs=1e-8)
    assert check(desk) == []


def test_flatten_takes_resting_profit_sales_off_the_book_first():
    fk, desk, pos = open_trade()
    fk.limit_fills = False
    tick(desk, fk, pos.take_profit * 1.001)
    desk.flatten("operator")
    run(desk.bus.drain())
    assert pos.is_flat and fk.ordertypes[-1] == "market"


def test_strategy_exits_of_a_winner_are_calm_too():
    fk, desk, pos = open_trade()
    approved = capture(desk, "order.approved")
    desk.settings.exit_only_in_profit = False
    tick(desk, fk, pos.avg_price * 1.03)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 0.9, "trend over", "quant", SignalKind.ENTRY))
    run(desk.bus.drain())
    assert approved and approved[-1].reduce_only and approved[-1].calm


def test_practice_and_other_venues_are_unchanged():
    from tests.helpers import FakeLiveBroker
    from tests.test_aggressive import buy, make
    d = make()
    d.live_broker = FakeLiveBroker(cash=10_000.0)
    pos = buy(d)
    assert pos.qty > 0  # (paper fills at once; only Kraken gets resting orders)


def test_off_by_default_profit_sales_go_at_market():
    fk, desk = live_kraken()
    assert desk.settings.maker_exits is False
    desk.settings.runners, desk.settings.scale_out_fraction = False, 0.0
    pos = kbuy(desk)
    tick(desk, fk, pos.take_profit * 1.001)
    assert pos.is_flat and fk.ordertypes[-1] == "market"


def test_a_stop_cancels_a_resting_buy_instead_of_waiting_behind_it():
    fk, desk = live_kraken()
    fk.limit_fills = False
    exe = desk.bots["execution"]
    kbuy(desk)  # the buy rests at the bid
    resting = next(iter(exe.working.values()))
    o = fk.orders[resting.broker_id]  # half of it filled
    vol = resting.qty * 0.5
    fk.bal["XXBT"] = fk.bal.get("XXBT", 0) + vol
    fk.bal["ZUSD"] -= vol * 59_994.0
    o.update(status="open", vol_exec=f"{vol:.8f}", cost=f"{vol * 59_994.0:.8f}", fee="0")
    run(exe.on_cycle())
    run(desk.bus.drain())
    desk.flatten("operator")  # urgent: take the rest of the buy off the book and sell what was bought
    run(desk.bus.drain())
    assert desk.portfolio.position("BTC-USD").is_flat and not exe.working
    assert fk.bal.get("XXBT", 0) == pytest.approx(0, abs=1e-8)  # no leftover buy after the flatten
