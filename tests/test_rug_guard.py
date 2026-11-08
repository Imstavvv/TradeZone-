"""The Rug Guard (asked for: "sells before rugged"), no market-cap minimum, and diversifying into buy areas."""

import time

from tests.helpers import capture, prime, run
from tests.test_new_bots import make, snap
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, SignalKind


def held(desk, sym="DOGE-USD", price=0.2):
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.95, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position(sym).qty > 0
    return desk.portfolio.position(sym)


def bars(desk, sym, prices, **kw):
    for p in prices:
        desk.prices[sym] = p
        desk.portfolio.mark(sym, p)
        snap(desk, sym, p, **kw)


def guard(desk):
    async def go():
        await desk.bots["rugguard"].on_cycle()
        await desk.bus.drain()
    run(go())


def test_a_crash_on_heavy_volume_is_sold_at_once_even_at_a_loss_then_not_bought_for_a_day():
    desk = make()
    pos = held(desk)
    bars(desk, "DOGE-USD", [0.2, 0.198, 0.195, 0.19, 0.185, 0.18], volume_ratio_5=4.0)  # -10% on 4x volume
    alerts = capture(desk, "alert")
    guard(desk)
    assert pos.qty == 0  # (sold - below break-even, before the -7% floor would have)
    assert any("Rug Guard sold all of DOGE-USD" in a.message for a in alerts)
    rej = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("DOGE-USD", Side.BUY, 0.95, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("DOGE-USD").qty == 0 and "Rug Guard sold" in rej[-1]["reason"]


def test_one_small_sign_alone_is_not_enough_two_are():
    desk = make()
    pos = held(desk)
    bars(desk, "DOGE-USD", [0.2, 0.199, 0.198, 0.197, 0.196, 0.193], volume_ratio_5=1.0)  # -3.5%: a slide only
    guard(desk)
    assert pos.qty > 0
    t = time.time()
    desk.bots["smartmoney"].tape["DOGE-USD"] = {"lean": -0.9, "sell_usd": 80_000, "buy_usd": 4_000, "at": t}
    guard(desk)
    assert pos.qty == 0  # (sliding + whales dumping)


def test_kraken_restricting_a_held_coin_sells_it():
    desk = make()
    pos = held(desk)
    desk.feed.restricted = {"DOGE-USD": time.time()}
    guard(desk)
    assert pos.qty == 0


def test_bitcoin_and_the_switch_off_are_left_alone():
    desk = make()
    pos = held(desk, "BTC-USD", 60_000.0)
    bars(desk, "BTC-USD", [60_000, 59_000, 58_000, 57_000, 55_000, 53_000], volume_ratio_5=5.0)
    guard(desk)
    assert pos.qty > 0  # (Bitcoin can't be rugged)
    desk = make(rug_guard=False)
    pos = held(desk)
    bars(desk, "DOGE-USD", [0.2, 0.198, 0.195, 0.19, 0.185, 0.18], volume_ratio_5=4.0)
    guard(desk)
    assert pos.qty > 0


def test_a_rug_sale_is_the_only_bot_sale_allowed_below_break_even():
    desk = make()
    pos = held(desk)
    desk.prices["DOGE-USD"] = 0.195
    desk.portfolio.mark("DOGE-USD", 0.195)
    rej = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("DOGE-USD", Side.SELL, 1, "x", "pm", SignalKind.EXIT))
    run(desk.bus.drain())
    assert pos.qty > 0 and "never sell at a loss" in rej[-1]["reason"]


def test_kraken_restrictions_are_spotted_only_on_a_whole_list():
    from tests.test_all_kraken_coins import kraken
    from tradezone.market.kraken import KrakenFeed
    coins = {f"C{i}": 5e6 for i in range(20)}
    symbols = []
    feed = KrakenFeed(symbols, client=kraken(coins), min_meme_volume_usd=1e6, all_coins=True)
    run(feed.start())
    del coins["C3"]
    run(feed.rescan())
    assert list(feed.restricted) == ["C3-USD"]
    coins.clear()  # (a broken read: everything "gone") - never a wave of rug sales
    run(feed.rescan())
    assert list(feed.restricted) == ["C3-USD"]
    run(feed.client.aclose())


def test_no_market_cap_minimum():
    s = Settings()
    assert s.discovery_min_mcap == 0
    from tradezone.bots.trends import TrendBot
    bot = TrendBot.__new__(TrendBot)
    bot.desk = type("D", (), {"settings": s})()
    assert bot.promising(20_000, 150_000)  # (a $20k coin trading enough)
    assert not bot.promising(20_000, 5_000)  # (volume still needed)


def test_diversify_pays_for_a_buy_area_from_the_biggest_trade_in_profit():
    from tradezone.core.models import Fill
    for on, donor in ((True, "SOL-USD"), (False, "XRP-USD")):
        desk = make(diversify_rotation=on)
        for sym, px, qty in (("SOL-USD", 100.0, 6.0), ("XRP-USD", 2.0, 20.0)):  # SOL $600, XRP $40: both in profit
            prime(desk, sym, px, atr=px * 0.004)
            desk.portfolio.apply_fill(Fill("o", sym, Side.BUY, qty, px, 0.0))
            desk.prices[sym] = px * 1.2
            desk.portfolio.mark(sym, px * 1.2)
        desk.bots["quant"].scores = {"SOL-USD": {"ensemble": 0.5}, "XRP-USD": {"ensemble": -0.5}}  # (XRP weakest)
        assert desk.bots["risk"]._fund_from("ETH-USD", 30.0) == donor
