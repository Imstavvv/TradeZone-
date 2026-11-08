"""Every timeframe (asked for): 5m / 15m / 1h / 4h / daily candles for every coin, in every snapshot."""

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.core.timeframes import Timeframes
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

T0 = 1_800_000_000 - 1_800_000_000 % 86_400  # (midnight UTC: whole candles)


def test_one_minute_bars_become_every_timeframe():
    tf = Timeframes()
    for i in range(10):  # ten 1-minute bars
        tf.update(T0 + 60 * i, 100 + i + 0.5, 100 + i - 0.5, 100 + i, 2.0)
    f = tf.frames["5m"]
    assert len(f.closed) == 1 and f.closed[0][1:] == [100, 104.5, 99.5, 104, 10.0]  # open, high, low, close, volume
    assert f.cur[4] == 109 and tf.frames["1h"].closed == type(f.closed)(maxlen=300)  # the hour is still forming


def test_the_trend_on_each_timeframe_is_read():
    tf = Timeframes()
    for i in range(60 * 30):  # 30 hours rising
        tf.update(T0 + 60 * i, 100 * 1.0005 ** i, 100 * 1.0005 ** i, 100 * 1.0005 ** i)
    v = tf.view()
    assert v["tf_1h_trend"] == 1 and v["tf_5m_trend"] == 1 and v["tf_1h_rsi"] > 70
    assert v["mtf_up"] >= 3 and v["mtf_score"] > 0 and 0.9 <= v["tf_1h_range_pos"] <= 1.0


def test_the_exchanges_long_history_seeds_the_timeframes_and_4h_comes_from_1h():
    s = Settings(feed="sim", symbols=["BTC-USD"])
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    scout = d.bots["scout"]
    hourly = [{"ts": T0 + 3600 * i, "open": 100 - i * 0.1, "high": 100 - i * 0.1 + 0.2, "low": 100 - i * 0.1 - 0.2,
               "close": 100 - i * 0.1, "volume": 5} for i in range(48)]  # two days falling
    scout.seed_timeframe("BTC-USD", "1h", hourly)
    b = scout.books["BTC-USD"]
    assert len(b.tf.frames["1h"].closed) == 48 and len(b.tf.frames["4h"].closed) == 12
    assert b.tf.view()["tf_1h_trend"] == -1 and b.tf.frames["4h"].closed[0][2] == pytest.approx(100.2)


def test_every_snapshot_carries_the_timeframes():
    s = Settings(feed="sim", symbols=["BTC-USD"])
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    snaps = capture(d, "market.snapshot")
    for i in range(80):
        d.bus.publish("market.tick", Tick("BTC-USD", 100 + i * 0.1, 1.0, T0 + 300 * i))
    run(d.bus.drain())
    assert snaps and {"tf_5m_trend", "tf_1h_rsi", "mtf_score"} <= set(snaps[-1].indicators)


def test_the_higher_timeframe_filter_blocks_a_buy_against_the_hourly_trend():
    s = Settings(feed="sim", symbols=["BTC-USD"], mtf_filter="1h")
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    prime(d, "BTC-USD", 50_000.0, atr=250.0)
    d.bots["risk"].snapshots["BTC-USD"].indicators["tf_1h_trend"] = -1
    rejected = capture(d, "signal.rejected")
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    assert d.portfolio.position("BTC-USD").is_flat and "1h trend is down" in rejected[-1]["reason"]
    assert Settings().mtf_filter == "off"  # (off until tested)


@pytest.mark.parametrize("mode,good,bad", [
    ("range", {"tf_1h_range_pos": 0.3}, {"tf_1h_range_pos": 0.8}),
    ("value", {"bb_mid": 101.0}, {"bb_mid": 95.0}),
    ("support", {"donchian_low": 99.0, "donchian_high": 110.0}, {"donchian_low": 90.0, "donchian_high": 101.0}),
    ("cool", {"tf_1h_rsi": 45, "tf_15m_rsi": 50}, {"tf_1h_rsi": 72, "tf_15m_rsi": 50}),
])
def test_a_buy_waits_for_the_best_buy_area(mode, good, bad):
    from tradezone.bots.quant import QuantBot
    assert QuantBot.buy_area_wait(mode, 100.0, good) is None
    assert QuantBot.buy_area_wait(mode, 100.0, bad)
    assert QuantBot.buy_area_wait("off", 100.0, bad) is None and Settings().buy_area == "off"


def test_volatility_targeting_sizes_calm_coins_bigger_and_wild_ones_smaller():
    def size(vol_target):
        s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD", "SOL-USD"], vol_target=vol_target, wide_stop=False)
        s.risk.symbol_cooldown_s = 0
        s.risk.risk_per_trade = 0.002  # (small trades: the size cap doesn't decide here)
        d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
        for sym, px in (("BTC-USD", 50_000.0), ("ETH-USD", 3_000.0), ("SOL-USD", 150.0)):
            prime(d, sym, px, atr=px * 0.005)
        d.bots["risk"].snapshots["BTC-USD"].indicators["volatility"] = 0.001  # the calm one
        d.bots["risk"].snapshots["ETH-USD"].indicators["volatility"] = 0.002
        d.bots["risk"].snapshots["SOL-USD"].indicators["volatility"] = 0.004  # the wild one
        out = {}
        for sym in ("BTC-USD", "SOL-USD"):
            d.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
            run(d.bus.drain())
            p = d.portfolio.position(sym)
            out[sym] = p.qty * p.avg_price
        return out
    off, on = size(False), size(True)  # (explicitly off / on)
    assert on["BTC-USD"] > off["BTC-USD"] * 1.2 and on["SOL-USD"] < off["SOL-USD"] * 0.8
    assert Settings().vol_target is True and Settings().trail_atr_mult == 0.0  # (tested: targeting on, chandelier off)


def test_a_market_wide_hourly_downtrend_stops_new_buys_one_falling_coin_doesnt():
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"], market_down_share=0.6,
                 falling_buys="block")  # (the block itself)
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    for sym, px in (("BTC-USD", 50_000.0), ("ETH-USD", 3_000.0), ("SOL-USD", 150.0), ("XRP-USD", 0.6)):
        prime(d, sym, px, atr=px * 0.005)
    snaps = d.bots["risk"].snapshots
    for sym in ("ETH-USD", "SOL-USD", "XRP-USD"):
        snaps[sym].indicators["tf_1h_trend"] = -1  # three of four falling: a broad slide
    snaps["BTC-USD"].indicators["tf_1h_trend"] = 1
    rejected = capture(d, "signal.rejected")
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    assert d.portfolio.position("BTC-USD").is_flat and "most of the market" in rejected[-1]["reason"]
    for sym in ("SOL-USD", "XRP-USD"):
        snaps[sym].indicators["tf_1h_trend"] = 1  # now only ETH falls
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    assert d.portfolio.position("BTC-USD").qty > 0
    assert Settings().market_down_share == 0.5  # (on: it did as well or better in every market on both halves)
