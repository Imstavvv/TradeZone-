"""Broker refusals: the ones Kraken never processed are sent again; buys that can only be refused aren't sent."""

import httpx
import pytest

from tests.helpers import capture, prime, run
from tests.test_kraken import SECRET, FakeKraken
from tradezone.brokers.kraken import KrakenBroker, KrakenError
from tradezone.core.models import Order, OrderStatus, Side, Signal


def flaky(fk, errors):
    """A Kraken that refuses the next AddOrder(s) with these errors, then behaves."""
    queue = list(errors)

    def handler(req):
        if req.url.path.endswith("/AddOrder") and queue:
            fk.calls.append("AddOrder")
            return httpx.Response(200, json={"error": [queue.pop(0)], "result": {}})
        return fk.handler(req)
    b = KrakenBroker("KEY", SECRET, client=httpx.AsyncClient(base_url="https://api.kraken.com",
                                                             transport=httpx.MockTransport(handler)))
    b.RETRY_SAFE = (("Invalid nonce", 0.0), ("Rate limit exceeded", 0.0))  # (no waiting in tests)
    return b


def test_nonces_are_microseconds_and_always_rise():
    b = KrakenBroker("KEY", SECRET)
    a, c = b._next_nonce(), b._next_nonce()
    assert c > a and a > 1_600_000_000_000_000  # (microseconds since 1970)


@pytest.mark.parametrize("err", ["EAPI:Invalid nonce", "EOrder:Rate limit exceeded"])
def test_a_refusal_kraken_never_processed_is_sent_again_and_fills_once(err):
    fk = FakeKraken(usd=1_000.0)
    b = flaky(fk, [err])
    run(b.load_pairs())
    o = Order("BTC-USD", Side.BUY, 0.001, "t1")
    fills = run(b.submit(o))
    assert o.status is OrderStatus.FILLED and sum(f.qty for f in fills) == pytest.approx(0.001)
    assert fk.calls.count("AddOrder") == 2 and len(fk.orders) == 1  # refused once, then one order only


def test_other_refusals_are_not_sent_again():
    fk = FakeKraken(usd=1_000.0)
    b = flaky(fk, ["EGeneral:Permission denied"])
    with pytest.raises(KrakenError, match="Permission denied"):
        run(b._private("AddOrder", pair="XBTUSD", type="buy", ordertype="market", volume="0.001"))
    assert fk.calls.count("AddOrder") == 1


def test_a_buy_the_free_cash_cant_make_big_enough_is_skipped_quietly():
    from tests.test_adopt_coins import live_desk
    fk = FakeKraken(usd=1_000.0)
    d = live_desk(fk)
    d.portfolio.cash = 3.0  # $3 free: under Kraken's 0.0001 BTC minimum ($6)
    alerts, sent = capture(d, "alert"), len([c for c in fk.calls if c == "AddOrder"])
    prime(d, "BTC-USD", 60_000.0, atr=300.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    assert [c for c in fk.calls if c == "AddOrder"][sent:] == []  # never sent to be refused
    assert not any("broker rejected" in a.message for a in alerts)


def test_refusals_are_explained_in_plain_words():
    from tradezone.bots.execution import ExecutionBot
    assert "another app" in ExecutionBot.plain_refusal("kraken: EAPI:Invalid nonce")
    assert "open order" in ExecutionBot.plain_refusal("kraken: EOrder:Insufficient funds")
    assert "Create & modify orders" in ExecutionBot.plain_refusal("kraken: EGeneral:Permission denied")
    assert ExecutionBot.plain_refusal("something new") == ""
