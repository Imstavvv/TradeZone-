"""Currency pairs on Kraken (EUR/USD, GBP/USD, AUD/USD): off by default; their own fee, target and stop."""

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.kraken import parse_pairs
from tradezone.market.simulated import SimulatedFeed


def test_off_by_default_and_nothing_added():
    s = Settings(feed="kraken", symbols=["BTC-USD"])
    s.validate()
    assert s.forex is False and not any(s.is_forex(x) for x in s.symbols)


def test_switched_on_kraken_watches_the_pairs_with_the_dollar_second():
    s = Settings(feed="kraken", symbols=["BTC-USD"], forex=True, forex_pairs=["EUR", "GBP", "AUD", "JPY"])
    s.validate()
    assert {"EUR-USD", "GBP-USD", "AUD-USD"} <= set(s.symbols)
    assert "JPY-USD" not in s.symbols  # (USD/JPY has the dollar first: not supported)


def test_only_kraken_gets_them():
    s = Settings(feed="coinbase", symbols=["BTC-USD"], forex=True)
    s.validate()
    assert "EUR-USD" not in s.symbols


def test_their_own_fee_target_and_stop_and_no_ladder():
    s = Settings(feed="sim", symbols=["EUR-USD", "BTC-USD"], forex=True)
    assert s.is_forex("EUR-USD") and not s.is_forex("BTC-USD") and not s.is_meme("EUR-USD")
    assert s.fee_bps("EUR-USD") == 20.0 and s.fee_bps("BTC-USD") == s.crypto_fee_bps  # Kraken's FX schedule: 0.20% a side
    assert s.plan_pcts("EUR-USD", True) == (s.forex_stop_pct, s.forex_target_pct)
    d = TradingDesk(Settings(feed="sim", symbols=["EUR-USD", "BTC-USD"], starting_cash=100.0), feed=SimulatedFeed(["EUR-USD", "BTC-USD"]),
                    journal=Journal(":memory:"))
    assert d.in_focus("EUR-USD") is None and d.in_focus("BTC-USD") is not None  # (a $100 balance: BTC waits, EUR/USD doesn't)


def test_kraken_lists_a_currency_pair_like_any_usd_pair():
    pairs = parse_pairs({"ZEURZUSD": {"altname": "EURUSD", "wsname": "EUR/USD", "base": "ZEUR", "lot_decimals": 8, "ordermin": "5"},
                         "ZUSDZJPY": {"altname": "USDJPY", "wsname": "USD/JPY", "base": "ZUSD", "lot_decimals": 8, "ordermin": "5"}})
    assert "EUR-USD" in pairs and pairs["EUR-USD"]["base"] == "ZEUR" and "USD-JPY" not in pairs
