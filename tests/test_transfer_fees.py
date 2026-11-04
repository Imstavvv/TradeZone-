"""Profit-taking keeps transfer fees in mind; Kraken accounts use Kraken's own prices."""

import time

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, SignalKind
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(**kw):
    s = Settings(feed="coinbase", symbols=["BTC-USD", "ETH-USD"], withdrawal_cooldown_h=0, **kw)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def test_transfer_fee_comes_from_your_setting_or_your_cheapest_wallet():
    desk = make()
    assert desk.transfer_fee() == 1.0  # nothing linked: a typical $1
    desk.wallets.link("BTC", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    assert desk.transfer_fee() == 5.0  # Bitcoin withdrawals cost a few dollars
    desk.wallets.link("XRP", "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", tag="12345")
    assert desk.transfer_fee() == pytest.approx(0.2)  # the cheapest way out is used
    assert make(cashout_fee_usd=3.0).transfer_fee() == 3.0  # your own number wins
    assert desk.min_take() == pytest.approx(0.02)  # each profit-take must earn 10% of one transfer


def test_small_profit_takes_wait_until_they_are_worth_a_transfer():
    desk2 = make(cashout_fee_usd=100.0)  # $10 per take: +$9 gross, ~+$6 after both fees, isn't worth it yet
    prime(desk2, "BTC-USD", 50_000.0, atr=250.0)
    p2 = desk2.portfolio.position("BTC-USD")
    p2.qty, p2.avg_price, p2.last_price, p2.opened_ts, p2.stop_price = 0.01, 50_000.0, 50_900.0, time.time(), 48_500.0
    rej2 = capture(desk2, "signal.rejected")
    desk2.prices["BTC-USD"] = 50_900.0
    desk2.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 0.9, "strategy says sell", "quant"))
    run(desk2.bus.drain())
    assert p2.qty == pytest.approx(0.01) and "transfer fee" in rej2[-1]["reason"]
    # stops still always work: protection never waits for a fee
    desk2.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 1.0, "stop", "portfolio", SignalKind.EXIT))
    run(desk2.bus.drain())
    assert p2.is_flat


def test_transfer_fees_never_stop_a_small_account_from_trading():
    desk = make(cashout_fee_usd=5.0, starting_cash=50.0, focus_small_coins=False)  # a $5 fee on a $50 account
    assert desk.min_take() == pytest.approx(0.05)  # 0.1% of the balance, not $0.50 per profit-take
    prime(desk, "BTC-USD", 50_000.0, atr=250.0)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("BTC-USD").qty > 0  # a fee is paid per cash-out, not per trade


def test_take_home_profit_shows_what_is_left_after_moving_it_out():
    desk = make(cashout_fee_usd=2.0)
    c = desk.cashout_summary()
    assert c["banked"] == 0 and c["take_home"] == 0 and not c["worth_it"]
    assert desk.state()["cashout"]["transfer_fee"] == 2.0


def test_a_kraken_account_uses_krakens_own_prices():
    s = Settings(kraken_key="k", kraken_secret="s")
    s.validate()
    assert s.feed == "kraken" and s.feed_choice == "auto" and "BTC-USD" in s.symbols
    assert Settings(kraken_key="k", kraken_secret="s", feed="coinbase").feed == "coinbase"  # an explicit choice wins
    with pytest.raises(ValueError, match="Kraken only quotes crypto"):
        Settings(feed="kraken", symbols=["AAPL"]).validate()


def test_sending_an_amount_the_transfer_fee_would_mostly_eat_is_refused():
    from tradezone.wallets import WalletError
    desk = make()
    desk.prices["BTC-USD"] = 60_000.0
    w = desk.wallets.link("BTC", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    with pytest.raises(WalletError, match="transfer fee"):
        desk.wallets.withdraw(w.id, "BTC", 0.0001)  # ~$6 with a ~$5 fee
    assert desk.wallets.withdraw(w.id, "BTC", 0.0003).status == "completed"  # ~$18: fine (practice)
