"""Shorting: profit from falling prices with the same stops, targets and fees - only where it really works."""

import httpx
import pytest

from tests.helpers import capture, prime, run
from tradezone.brokers.alpaca import AlpacaBroker
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(**kw):
    kw.setdefault("symbols", ["AAPL", "TSLA", "BTC-USD", "DOGE-USD"])
    kw.setdefault("wide_stop", False)
    kw.setdefault("shorting", "auto")  # these tests check shorting itself (it's off by default)
    s = Settings(never_sell_at_loss=False, feed="sim", **kw)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def short(desk, sym="AAPL", price=200.0):
    prime(desk, sym, price, atr=price * 0.005)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal(sym, Side.SELL, 0.9, "bearish", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym), rejected


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def test_a_stock_short_uses_the_same_plan_mirrored_and_profits_when_it_falls():
    desk = make()
    pos, _ = short(desk)
    assert pos.qty < 0 and pos.qty == int(pos.qty)  # whole shares only
    fees = desk.settings.round_trip_cost("AAPL", 200.0)
    assert pos.stop_price == pytest.approx(200 * 1.03)  # -3% stop, mirrored: above the price
    assert pos.take_profit == pytest.approx(200 * 0.94 - fees)  # +6% after fees, mirrored: below
    trades = capture(desk, "trade.closed")
    tick(desk, "AAPL", 187.0)  # fell 6.5%: the target is hit
    assert trades and trades[-1].pnl > 0  # profit from the fall


def test_a_short_that_goes_the_wrong_way_is_cut_at_its_stop():
    desk = make()
    pos, _ = short(desk)
    start = desk.portfolio.equity
    trades = capture(desk, "trade.closed")
    tick(desk, "AAPL", 207.0)  # rose 3.5%: through the stop
    assert pos.is_flat and trades[-1].pnl < 0
    assert start - desk.portfolio.equity < start * 0.012  # the loss stays about one trade's risk


@pytest.mark.parametrize("sym,price,why", [("BTC-USD", 50_000.0, "crypto can't be shorted"),
                                           ("DOGE-USD", 0.2, "meme coins are never shorted")])
def test_never_shorts_crypto_or_meme_coins(sym, price, why):
    pos, rejected = short(make(), sym, price)
    assert pos.is_flat and why in rejected[-1]["reason"]


def test_shorting_can_be_switched_off():
    pos, rejected = short(make(shorting="off"))
    assert pos.is_flat and "shorting is off" in rejected[-1]["reason"]


def test_practice_follows_a_linked_account_that_cannot_short():
    from tradezone.brokers.kraken import KrakenBroker
    desk = make()
    desk.live_broker = KrakenBroker("k", "c2VjcmV0")  # Kraken is used spot-only: no shorts
    pos, rejected = short(desk)
    assert pos.is_flat and "practice follows your Kraken account" in rejected[-1]["reason"]
    run(desk.live_broker.close())


def alpaca(shorting=True, equity=5_000.0):
    def handler(req):
        if req.url.path == "/v2/account":
            return httpx.Response(200, json={"cash": "5000", "equity": str(equity), "shorting_enabled": shorting})
        sym = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"symbol": sym, "tradable": True, "shortable": True,
                                         "easy_to_borrow": sym == "AAPL"})
    b = AlpacaBroker("AK", "s", paper=False, client=httpx.AsyncClient(base_url="https://api.alpaca.markets",
                                                                       transport=httpx.MockTransport(handler)))
    b.symbols = ["AAPL", "TSLA", "BTC-USD"]
    return b


def test_alpaca_shorts_only_easy_to_borrow_stocks_on_a_margin_account():
    b = alpaca()
    run(b.refresh_market())
    assert b.can_short("AAPL") and not b.can_short("TSLA") and not b.can_short("BTC-USD")
    small = alpaca(equity=1_500.0)  # under Alpaca's $2,000 margin minimum
    run(small.refresh_market())
    assert not small.can_short("AAPL")
    cash = alpaca(shorting=False)  # a cash account
    run(cash.refresh_market())
    assert not cash.can_short("AAPL")


def test_short_sale_cash_is_set_aside_not_spent_or_sent():
    desk = make(starting_cash=10_000.0)
    pos, _ = short(desk)
    owed = abs(pos.market_value)
    assert desk.portfolio.cash > 10_000.0  # the sale put cash in the account...
    assert desk.free_cash() == pytest.approx(desk.portfolio.cash - owed)  # ...but it's owed back
