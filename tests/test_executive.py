"""Executive (head trader): final real-time authority over every trade."""

import time

import pytest

from tests.helpers import capture, prime, run
from tradezone.core.models import OrderRequest, Side, Signal
from tradezone.core.portfolio import ClosedTrade


def approve(desk, req):
    desk.bus.publish("order.approved", req, source="risk")
    run(desk.bus.drain())


def entry(sym="AAPL", side=Side.BUY, ref=100.0, stop=98.5, tp=103.0, qty=10.0):
    return OrderRequest(sym, side, qty, "test", "sig-x", False, stop, tp, ref)


def test_every_trade_goes_through_the_executive(desk):
    auth = capture(desk, "order.authorized")
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert len(auth) == 1 and desk.portfolio.position("AAPL").qty > 0
    assert desk.bots["executive"].latencies  # decision latency measured


def test_executor_ignores_unauthorised_orders(desk):
    prime(desk)
    desk.set_bot_enabled("executive", False)
    approve(desk, entry())
    assert desk.bots["execution"].orders == {}  # no Executive, no trades


def test_no_chasing_a_price_that_ran(desk):
    prime(desk, price=101.0)  # planned at 100 with stop 98.5 (1.5 risk): price already +0.67R
    held = capture(desk, "order.held")
    approve(desk, entry())
    assert "not chasing" in held[0]["reason"] and not desk.bots["execution"].orders


def test_holds_when_price_is_through_the_stop(desk):
    prime(desk, price=98.0)
    held = capture(desk, "order.held")
    approve(desk, entry())
    assert "through the stop" in held[0]["reason"]


def test_holds_on_stale_prices_and_offline_team(desk):
    from tradezone.bots.scout import _Book
    prime(desk)
    book = desk.bots["scout"].books.setdefault("AAPL", _Book())
    book.last_ts = time.time() - 120
    held = capture(desk, "order.held")
    approve(desk, entry())
    assert "stale" in held[-1]["reason"]
    book.last_ts = time.time()
    desk.set_bot_enabled("sentinel", False)
    approve(desk, entry())
    assert "Sentinel is offline" in held[-1]["reason"]


def test_limits_correlated_crypto_bets(desk):
    desk.settings.exec_max_correlated = 3
    for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
        desk.portfolio.position(sym).qty = 1.0
        desk.portfolio.position(sym).avg_price = 100.0
    prime(desk, "XRP-USD", 2.5, atr=0.01)
    desk.settings.symbols.append("XRP-USD")
    held = capture(desk, "order.held")
    approve(desk, entry("XRP-USD", ref=2.5, stop=2.45, tp=2.6))
    assert "correlated" in held[0]["reason"]


def test_defensive_stance_after_losing_streak(desk):
    ex = desk.bots["executive"]
    alerts = capture(desk, "alert")
    for _ in range(3):
        desk.portfolio.closed_trades.append(ClosedTrade("AAPL", "long", 1, 100, 99, -1.0))
    ex.review_stance()
    run(desk.bus.drain())
    assert ex.mode == "defensive" and "3 losses in a row" in alerts[-1].message
    prime(desk)
    auth = capture(desk, "order.authorized")
    approve(desk, entry(qty=10))
    assert auth[0].qty == pytest.approx(5)  # half size
    desk.bots["risk"].strength_by_signal["weak"] = 0.4  # below the 0.45 defensive bar
    held = capture(desk, "order.held")
    weak = entry()
    weak.signal_id = "weak"
    approve(desk, weak)
    assert "conviction" in held[0]["reason"]
    desk.portfolio.closed_trades.append(ClosedTrade("AAPL", "long", 1, 100, 102, 2.0))
    ex.review_stance()
    assert ex.mode == "normal"


def test_exits_always_pass_instantly(desk):
    prime(desk)
    desk.feed_down = True  # even with no market data
    desk.set_bot_enabled("sentinel", False)
    desk.portfolio.position("AAPL").qty = 5
    auth = capture(desk, "order.authorized")
    approve(desk, OrderRequest("AAPL", Side.SELL, 5, "stop hit", "s", reduce_only=True))
    assert len(auth) == 1 and desk.portfolio.position("AAPL").is_flat


def test_defensive_stance_cools_off(desk):
    ex = desk.bots["executive"]
    old = time.time() - 31 * 60
    for _ in range(4):
        desk.portfolio.closed_trades.append(ClosedTrade("AAPL", "long", 1, 100, 99, -1.0, ts=old))
    ex.review_stance()
    assert ex.mode == "normal"  # the streak is older than the 30-minute cool-off
    desk.portfolio.closed_trades[-1].ts = time.time()
    ex.review_stance()
    assert ex.mode == "defensive"
