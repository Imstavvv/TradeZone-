"""The bots watch only what Kraken has to offer (asked for)."""

from tradezone.config import Settings


def test_no_keys_at_all_means_krakens_coins_prices_and_fees():
    s = Settings()
    s.validate()
    assert s.kraken_only and s.feed == "kraken" and s.venue() == "kraken"
    s.apply_venue_costs()
    assert s.fee_bps("BTC-USD") == s.kraken_fee_bps  # (practice charges Kraken's fee)


def test_an_explicit_feed_or_another_real_money_venue_is_respected():
    s = Settings(feed="sim")
    s.validate()
    assert s.feed == "sim"
    s = Settings(alpaca_live_key="k", alpaca_live_secret="x")
    assert s.venue() == "alpaca"  # (an Alpaca real-money account set up: its own list)
    s = Settings(kraken_only=False)
    assert s.venue() == "alpaca"  # (the old fallback, when switched off)
