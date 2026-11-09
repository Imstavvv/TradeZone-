"""Small market-cap and new coins first for buys (asked for)."""

import time

from tests.helpers import capture, prime, run
from tests.test_new_bots import make
from tradezone.config import Settings
from tradezone.core.models import Side, Signal


def test_which_coins_are_focus_coins():
    s = Settings()
    assert s.is_focus_coin("DOGE-USD") and s.is_focus_coin("KAITO-USD")  # (small market cap)
    assert not s.is_focus_coin("BTC-USD") and not s.is_focus_coin("SOL-USD") and not s.is_focus_coin("AAPL")
    s.new_coins["SOL"] = time.time()  # (pretend Kraken just listed it)
    assert s.is_focus_coin("SOL-USD")
    s.new_coins["SOL"] = time.time() - 8 * 86_400
    assert not s.is_focus_coin("SOL-USD")  # (a week on: no longer new)


def _buy(desk, sym, px):
    prime(desk, sym, px, atr=px * 0.004)
    rej = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym), rej


def test_only_mode_buys_small_and_new_coins_and_size_mode_buys_them_bigger():
    desk = make(small_focus="only")
    pos, rej = _buy(desk, "BTC-USD", 60_000.0)
    assert pos.qty == 0 and "large / mid-size coin" in rej[-1]["reason"]
    pos, _ = _buy(desk, "DOGE-USD", 0.2)
    assert pos.qty > 0 and desk.bots["risk"].metrics["focus_buys"] == 1
    small = make(small_focus="off")
    plain, _ = _buy(small, "DOGE-USD", 0.2)
    big = make(small_focus="size")
    bigger, _ = _buy(big, "DOGE-USD", 0.2)
    assert bigger.qty > plain.qty


def test_off_by_default_until_replayed():
    assert Settings().small_focus == "off"


def test_the_new_coins_list_survives_a_restart():
    desk = make()
    desk.store.save("new_coins", {"FRESH": time.time()})
    run(desk.bots["listings"].on_cycle())
    assert desk.settings.is_new_coin("FRESH-USD")


def test_a_buy_in_a_good_buy_area_gets_more_money_when_the_boost_is_on():
    from tests.test_new_bots import snap
    sizes = {}
    for boost in (1.0, 1.5):
        desk = make(area_boost=boost)
        desk.settings.risk.risk_per_trade = 0.001  # (small: the position cap mustn't bind)
        prime(desk, "ETH-USD", 3000.0, atr=12.0)
        snap(desk, "ETH-USD", 3000.0, rsi=30, atr=12.0)  # (oversold: a good buy area)
        desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.BUY, 0.9, "t", "quant"))
        run(desk.bus.drain())
        sizes[boost] = desk.portfolio.position("ETH-USD").qty
    assert sizes[1.5] > sizes[1.0] > 0
    assert Settings().area_boost == 1.0  # (off until replayed)
