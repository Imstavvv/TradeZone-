"""Never margin funds (asked for): borrowed money is never counted as the bots' to spend, and never traded."""

import httpx
import pytest

from tests.helpers import run
from tests.test_kraken import SECRET, FakeKraken
from tradezone.brokers.kraken import KrakenBroker, KrakenError


def kraken_with_credit():
    fk = FakeKraken(usd=1_000.0)

    def handler(req):
        if req.url.path.endswith("/BalanceEx"):
            fk.calls.append("BalanceEx")
            return httpx.Response(200, json={"error": [], "result": {
                "ZUSD": {"balance": "1000.0", "credit": "500.0", "credit_used": "100.0", "hold_trade": "0"}}})
        return fk.handler(req)
    b = KrakenBroker("KEY", SECRET, client=httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)))
    return fk, b


def test_a_credit_line_is_never_counted_as_free_cash():
    fk, b = kraken_with_credit()
    run(b.load_pairs())
    assert run(b.available_cash()) == pytest.approx(900.0)  # $1,000 owned - $100 borrowed; the $500 line ignored


def test_an_order_on_margin_is_refused_before_it_is_sent():
    fk, b = kraken_with_credit()
    for extra in ({"leverage": "2"}, {"margin": "true"}, {"reduce_only": "true"}):
        with pytest.raises(KrakenError, match="never trades on margin"):
            run(b._private("AddOrder", pair="XBTUSD", type="buy", ordertype="market", volume="0.001", **extra))
    assert "AddOrder" not in fk.calls


def test_alpaca_negative_cash_a_margin_loan_counts_as_none():
    from tradezone.brokers.alpaca import AlpacaBroker

    def handler(req):
        return httpx.Response(200, json={"cash": "-250.00", "equity": "1800.00", "buying_power": "3600.00",
                                         "daytrade_count": 0})
    b = AlpacaBroker("AK", "S", paper=False, client=httpx.AsyncClient(base_url="https://api.alpaca.markets",
                                                                        transport=httpx.MockTransport(handler)))
    assert run(b.account())["cash"] == 0.0  # (buying power, which includes borrowing, is never used)
