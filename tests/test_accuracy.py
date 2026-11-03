import asyncio

import pytest

from tests.helpers import prime, run
from tradezone.brokers.paper import PaperBroker
from tradezone.core.models import Fill, OrderRequest, Side, Signal


class BadBroker(PaperBroker):
    """Broker that misbehaves in configurable ways."""

    def __init__(self, mode):
        super().__init__(0, 0)
        self.mode = mode

    async def submit(self, order):
        fills = await super().submit(order)
        f = fills[0]
        if self.mode == "slippage":
            f.price *= 1.02
        elif self.mode == "overfill":
            f.qty *= 2
        elif self.mode == "wrong_side":
            f.side = f.side.opposite
        return [f]


def test_clean_run_is_100_percent_accurate(desk):
    async def go():
        await desk.start()
        for _ in range(1500):
            await desk.step()

    run(go())
    exe, sen = desk.bots["execution"], desk.bots["sentinel"]
    assert exe.metrics["fills"] > 0
    assert exe.accuracy == 1.0
    assert exe.metrics["integrity_violations"] == 0
    assert sen.books_balanced and sen.metrics["audit_checks"] == 1500
    assert not desk.paused


@pytest.mark.parametrize("mode", ["slippage", "overfill", "wrong_side"])
def test_bad_fills_are_caught_and_pause_trading(desk, mode):
    desk.broker = BadBroker(mode)
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    exe = desk.bots["execution"]
    assert exe.metrics["integrity_violations"] == 1
    assert exe.accuracy < 1
    assert desk.paused and "integrity" in desk.pause_reason


def test_pretrade_guards(desk):
    prime(desk)
    exe = desk.bots["execution"]
    assert "approved symbol" in exe.pretrade_check(OrderRequest("DOGE", Side.BUY, 1, "", "s"))
    assert "invalid quantity" in exe.pretrade_check(OrderRequest("AAPL", Side.BUY, float("nan"), "", "s"))
    assert "fat-finger" in exe.pretrade_check(OrderRequest("AAPL", Side.BUY, 10_000, "", "s"))
    req = OrderRequest("AAPL", Side.BUY, 1, "", "s")
    desk.bus.publish("order.approved", req)
    desk.bus.publish("order.approved", req)  # replayed request
    run(desk.bus.drain())
    assert len(exe.orders) == 1


def test_audit_detects_ledger_tampering(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    sen = desk.bots["sentinel"]
    run(sen.on_cycle())
    assert sen.books_balanced
    desk.portfolio.cash += 500  # money appearing from nowhere
    run(sen.on_cycle())
    assert not sen.books_balanced and desk.paused


def test_duplicate_signal_ids_rejected(desk):
    prime(desk)
    sig = Signal("AAPL", Side.BUY, 0.9, "t", "quant")
    desk.bus.publish("signal.proposed", sig)
    desk.bus.publish("signal.proposed", sig)
    run(desk.bus.drain())
    assert len(desk.bots["execution"].orders) == 1


def test_broker_position_mismatch_pauses(desk):
    async def broker_positions():
        return {"AAPL": 999.0}

    desk.broker.positions = broker_positions
    problems = asyncio.run(desk.bots["portfolio"].reconcile_positions())
    assert problems and desk.paused


def test_simultaneous_exits_never_oversell(desk):
    """A partial take-profit and a full stop in the same instant must not flip the position short."""
    from tradezone.core.models import SignalKind
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    q = desk.portfolio.position("AAPL").qty
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 1, "partial", "portfolio", SignalKind.EXIT, fraction=0.5))
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 1, "stop", "portfolio", SignalKind.EXIT))
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 1, "flatten", "portfolio", SignalKind.EXIT))
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat
    sold = sum(o.filled_qty for o in desk.bots["execution"].orders.values() if o.reduce_only)
    assert sold == pytest.approx(q)


def test_unexpected_short_pauses_trading(desk):
    desk.bus.publish("order.filled", Fill("x", "AAPL", Side.SELL, 1, 100))
    run(desk.bus.drain())
    run(desk.bots["sentinel"].on_cycle())
    assert desk.paused and "short" in desk.pause_reason
