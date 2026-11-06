"""Shared helpers and the live-trading fixes from the clean-up patch."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.helpers import FakeLiveBroker, capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import OrderRequest, Side, Signal
from tradezone.core.util import (
    is_closing,
    norm_symbol,
    round_qty,
    us_market_open,
)
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app

NY = ZoneInfo("America/New_York")


def test_util_helpers():
    assert round_qty(1.23456789, 60_000) == 1.234567 and round_qty(1.23456, 5) == 1.2345
    assert round_qty(7.9, 100, fractional=False) == 7.0 and round_qty(float("nan"), 1) == 0.0
    assert norm_symbol("btc/usd") == norm_symbol("BTC-USD") == "BTCUSD"
    assert is_closing(5, Side.SELL) and is_closing(-5, Side.BUY) and not is_closing(0, Side.SELL)


@pytest.mark.parametrize("when,open_", [
    (datetime(2026, 11, 2, 10, 0, tzinfo=NY), True),    # Monday morning
    (datetime(2026, 11, 2, 9, 29, tzinfo=NY), False),   # before the open
    (datetime(2026, 11, 2, 16, 0, tzinfo=NY), False),   # at the close
    (datetime(2026, 11, 7, 12, 0, tzinfo=NY), False),   # Saturday
])
def test_us_market_hours(when, open_):
    assert us_market_open(when) is open_


def live_desk(broker):
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s",
                 symbols=["BTC-USD", "ETH-USD", "AAPL"])
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker)


def test_stop_sells_what_the_broker_actually_holds():
    broker = FakeLiveBroker(cash=10_000)
    desk = live_desk(broker)
    prime(desk, "BTC-USD", 60_000.0, atr=300.0)
    run(desk.switch_mode("live"))
    prime(desk, "BTC-USD", 60_000.0, atr=300.0)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    book_qty = desk.portfolio.position("BTC-USD").qty
    broker._positions = {"BTCUSD": book_qty * 0.9975}  # Alpaca kept 0.25% of the coins as its fee
    desk.bus.publish("order.approved", OrderRequest("BTC-USD", Side.SELL, book_qty, "stop", "s", reduce_only=True))
    run(desk.bus.drain())
    exit_order = [o for o in desk.bots["execution"].orders.values() if o.reduce_only][-1]
    assert exit_order.qty == pytest.approx(book_qty * 0.9975)  # never more than the broker has


def test_reconcile_adopts_fee_sized_difference_and_keeps_audit_balanced():
    broker = FakeLiveBroker(cash=10_000)
    desk = live_desk(broker)
    run(desk.switch_mode("live"))
    prime(desk, "BTC-USD", 60_000.0, atr=300.0)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    q = desk.portfolio.position("BTC-USD").qty
    broker._positions = {"BTCUSD": q * 0.9975}
    pm = desk.bots["portfolio"]
    pm.last_fill_ts = 0
    run(pm.reconcile_positions())  # seen once: wait for the next check
    problems = run(pm.reconcile_positions())
    run(desk.bus.drain())
    assert problems == [] and not desk.paused
    assert desk.portfolio.position("BTC-USD").qty == pytest.approx(q * 0.9975)
    sentinel = desk.bots["sentinel"]
    assert sentinel.audit_books() == []  # independent ledger applied the same adjustment
    broker._positions = {"BTCUSD": q * 0.5}  # half sold outside TradeZone: the records follow the account
    run(pm.reconcile_positions())
    assert run(pm.reconcile_positions()) and not desk.paused
    run(desk.bus.drain())
    assert desk.portfolio.position("BTC-USD").qty == pytest.approx(q * 0.5) and sentinel.audit_books() == []


def test_executive_holds_stocks_when_market_closed(monkeypatch):
    import tradezone.core.util as util
    desk = live_desk(FakeLiveBroker())
    desk.feed.name = "alpaca"  # real-data feed
    monkeypatch.setattr(util, "us_market_open", lambda: False)
    prime(desk, "AAPL")
    held = capture(desk, "order.held")
    desk.bus.publish("order.approved", OrderRequest("AAPL", Side.BUY, 1, "t", "s", False, 98.5, 103, 100))
    run(desk.bus.drain())
    assert "market is closed" in held[0]["reason"]
    prime(desk, "BTC-USD", 60_000.0, atr=300.0)  # crypto trades around the clock
    auth = capture(desk, "order.authorized")
    desk.bus.publish("order.approved", OrderRequest("BTC-USD", Side.BUY, 0.01, "t", "s", False, 59_000, 62_000, 60_000))
    run(desk.bus.drain())
    assert auth


def test_csp_never_echoes_junk_host():
    desk = TradingDesk(Settings(feed="sim"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as c:
        csp = c.get("/", headers={"host": "evil.com; script-src *"}).headers["content-security-policy"]
        assert "script-src *" not in csp and "evil.comscript-src" in csp


def test_alpacas_clock_decides_holidays_and_early_closes(monkeypatch):
    """A weekday at 11:00 can still be closed (Thanksgiving): Alpaca's clock says so, and the bots wait."""
    import time as _t

    import tradezone.core.util as util
    desk = live_desk(FakeLiveBroker())
    desk.feed.name = "alpaca"
    monkeypatch.setattr(util, "us_market_open", lambda: True)  # by the plain weekday rule: open
    desk.live_broker.market_open = lambda: False  # Alpaca: closed today
    assert desk.stock_market_open() is False
    prime(desk, "AAPL")
    held = capture(desk, "order.held")
    desk.bus.publish("order.approved", OrderRequest("AAPL", Side.BUY, 1, "t", "s", False, 98.5, 103, 100))
    run(desk.bus.drain())
    assert "market is closed" in held[0]["reason"]
    # an Alpaca answer older than 10 minutes isn't trusted: the plain rule decides again
    from tradezone.brokers.alpaca import AlpacaBroker
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.clock_open, b.clock_at = False, _t.time() - 3600
    assert b.market_open() is None
    b.clock_at = _t.time()
    assert b.market_open() is False
