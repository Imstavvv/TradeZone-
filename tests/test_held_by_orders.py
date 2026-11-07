"""Cash and coins held by open orders aren't spendable: orders are sized to what Kraken really releases, so they fill."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_adopt_coins import live_desk
from tests.test_kraken import FakeKraken
from tradezone.core.models import Side, Signal, SignalKind


def adds(fk):
    return [c for c in fk.calls if c == "AddOrder"]


def test_a_buy_uses_only_the_cash_open_orders_dont_hold():
    fk = FakeKraken(usd=1_000.0)
    d = live_desk(fk, adopt_own_coins=False)
    fk.hold["ZUSD"] = 900.0  # your own resting buy order on Kraken holds $900 of it
    alerts = capture(d, "alert")
    prime(d, "BTC-USD", 60_000.0, atr=300.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    pos = d.portfolio.position("BTC-USD")
    assert pos.qty > 0 and pos.qty * 60_000 <= 100.0  # sized to the $100 Kraken releases...
    assert fk.bal["ZUSD"] >= 0 and not any("broker rejected" in a.message for a in alerts)  # ...so it filled


def test_a_sale_sells_what_is_free_when_an_open_order_holds_part():
    fk = FakeKraken(usd=1_000.0)
    d = live_desk(fk, adopt_own_coins=False)
    prime(d, "BTC-USD", 60_000.0, atr=300.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    pos = d.portfolio.position("BTC-USD")
    had = pos.qty
    fk.hold["XXBT"] = had * 0.4  # 40% held by an order placed on Kraken
    alerts = capture(d, "alert")
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 1.0, "out", "portfolio", SignalKind.EXIT, force=True))
    run(d.bus.drain())
    assert pos.qty == pytest.approx(had * 0.4, rel=1e-3)  # the free 60% sold (filled, not refused)
    assert not any("broker rejected" in a.message for a in alerts)


def test_a_sale_waits_when_everything_is_held():
    fk = FakeKraken(usd=1_000.0)
    d = live_desk(fk, adopt_own_coins=False)
    prime(d, "BTC-USD", 60_000.0, atr=300.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    pos = d.portfolio.position("BTC-USD")
    fk.hold["XXBT"] = pos.qty
    sent = len(adds(fk))
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 1.0, "out", "portfolio", SignalKind.EXIT, force=True))
    run(d.bus.drain())
    assert len(adds(fk)) == sent and pos.qty > 0  # not sent to be refused; tried again later


def test_what_is_free_is_asked_once_per_burst_and_again_after_an_order():
    from tests.test_kraken import broker
    fk = FakeKraken(usd=1_000.0)
    b = broker(fk)
    run(b.load_pairs())
    run(b.available_cash())
    run(b.available_cash())
    assert fk.calls.count("BalanceEx") == 1  # (Kraken's private calls are counted)
    from tradezone.core.models import Order
    run(b.submit(Order("BTC-USD", Side.BUY, 0.001, "z1")))
    run(b.available_cash())
    assert fk.calls.count("BalanceEx") == 2  # an order changes what's free: asked afresh
