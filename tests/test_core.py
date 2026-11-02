import asyncio

import pytest

from tradezone.core.bus import EventBus
from tradezone.core.indicators import EMA, RSI, efficiency_ratio
from tradezone.core.models import Fill, Side
from tradezone.core.portfolio import Portfolio


def test_bus_fifo_wildcards_and_isolation():
    bus = EventBus()
    seen = []
    bus.subscribe("order.*", lambda e: seen.append(("prefix", e.topic)))
    bus.subscribe("*", lambda e: seen.append(("all", e.topic)))

    def boom(e):
        raise RuntimeError("bad bot")

    errors = []
    bus.on_error = lambda owner, ev, exc: errors.append(owner)
    bus.subscribe("order.filled", boom, owner="faulty")

    def chain(e):
        bus.publish("order.filled", 1)

    bus.subscribe("order.approved", chain)
    bus.publish("order.approved", 0)
    asyncio.run(bus.drain())
    assert ("prefix", "order.approved") in seen and ("prefix", "order.filled") in seen
    assert [t for k, t in seen if k == "all"] == ["order.approved", "order.filled"]
    assert errors == ["faulty"]


def test_indicators():
    e = EMA(3)
    for x in [1, 2, 3, 4]:
        e.update(x)
    assert 3 < e.value < 4
    r = RSI(14)
    for x in range(1, 40):
        r.update(x)
    assert r.value == 100.0
    assert efficiency_ratio([1, 2, 3, 4]) == 1.0
    assert efficiency_ratio([1, 2, 1, 2, 1]) == 0.0


def test_portfolio_average_cost_and_round_trip():
    p = Portfolio(10_000)
    p.apply_fill(Fill("o1", "X", Side.BUY, 10, 100, 1.0))
    p.apply_fill(Fill("o2", "X", Side.BUY, 10, 110, 1.0))
    pos = p.position("X")
    assert pos.qty == 20 and pos.avg_price == pytest.approx(105)
    p.mark("X", 120)
    assert p.equity == pytest.approx(10_000 - 2100 - 2 + 2400)
    closed = p.apply_fill(Fill("o3", "X", Side.SELL, 20, 120, 1.0))
    assert closed.pnl == pytest.approx(15 * 20 - 1)
    assert pos.is_flat and pos.avg_price == 0
    assert p.cash == pytest.approx(10_000 + 300 - 3)


def test_portfolio_flip_through_zero():
    p = Portfolio(10_000)
    p.apply_fill(Fill("o1", "X", Side.BUY, 5, 100))
    closed = p.apply_fill(Fill("o2", "X", Side.SELL, 8, 90))
    assert closed.qty == 5 and closed.pnl == pytest.approx(-50)
    pos = p.position("X")
    assert pos.qty == -3 and pos.avg_price == 90
