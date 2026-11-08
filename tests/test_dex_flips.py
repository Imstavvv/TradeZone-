"""DEX coins for quick flips (asked for): DEX-born tokens on Kraken aim for a nearer target, after every fee."""

import pytest

from tests.helpers import prime, run
from tests.test_new_bots import make
from tradezone.config import Settings
from tradezone.core.models import Side, Signal


def test_which_coins_are_dex_coins():
    s = Settings()
    assert s.is_dex("BONK-USD") and s.is_dex("wif-usd") and s.is_dex("FARTCOIN-USD") and s.is_dex("JUP-USD")
    assert not s.is_dex("BTC-USD") and not s.is_dex("ETH-USD") and not s.is_dex("AAPL")


def test_dex_coins_flip_at_a_nearer_target_with_the_same_stop():
    s = Settings(dex_flips=True)
    assert s.plan_pcts("BONK-USD") == (s.stop_loss_pct, 0.04)
    assert s.plan_pcts("BTC-USD") == (s.major_stop_loss_pct, s.major_take_profit_pct)  # (others unchanged)
    s.dex_flips = False
    assert s.plan_pcts("BONK-USD") == (s.stop_loss_pct, s.take_profit_pct)


def test_a_dex_buy_gets_the_quick_target_after_fees_and_a_little_more_conviction():
    desk = make(dex_flips=True)
    desk.settings.symbols.append("BONK-USD")
    prime(desk, "BONK-USD", 0.00002, atr=0.0000001)
    desk.bus.publish("signal.proposed", Signal("BONK-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("BONK-USD")
    assert pos.qty > 0
    fees = desk.settings.round_trip_cost("BONK-USD", 0.00002)
    assert pos.take_profit == pytest.approx(0.00002 * 1.04 + fees, rel=0.05)
    assert pos.take_profit > 0.00002 * 1.04  # (every fee covered on top)
    assert desk.bots["risk"].metrics["dex_flip_buys"] == 1


def test_tokens_kraken_lists_later_become_dex_coins():
    s = Settings()
    assert not s.is_dex("NEWTOKEN-USD")
    s.dex_coins.append("NEWTOKEN")
    assert s.is_dex("NEWTOKEN-USD")
