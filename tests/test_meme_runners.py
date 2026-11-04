"""Crypto focus: -8% / +15% plan, runners that ride with a trailing stop in profit, meme coins, sniping."""

import httpx
import pytest

from tests.helpers import capture, prime, run
from tradezone.config import CRYPTO_SYMBOLS, Settings
from tradezone.core.models import Side, Signal, Snapshot, Tick
from tradezone.market.coinbase import CoinbaseFeed


def buy(desk, sym, price, strength=0.9):
    if sym not in desk.settings.symbols:
        desk.settings.symbols.append(sym)
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, strength, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def test_defaults_are_crypto_with_trusted_meme_coins():
    s = Settings(feed="coinbase")
    s.validate()
    assert s.symbols == CRYPTO_SYMBOLS and all(x.endswith("-USD") for x in s.symbols)
    assert {"DOGE-USD", "SHIB-USD", "PEPE-USD"} <= set(s.symbols)
    assert s.is_meme("PEPE-USD") and s.is_meme("doge/usd") and not s.is_meme("BTC-USD")
    assert (s.stop_method, s.stop_loss_pct, s.take_profit_pct) == ("percent", 0.08, 0.15)


def test_meme_coins_get_minus_8_plus_15_and_majors_minus_3_plus_6(desk):
    meme = buy(desk, "PEPE-USD", 0.00001)
    assert meme.qty > 0
    fees = desk.settings.round_trip_cost  # targets are profit AFTER the buy and sell fees
    assert meme.stop_price == pytest.approx(0.00001 * 0.92)
    assert meme.take_profit == pytest.approx(0.00001 * 1.15 + fees("PEPE-USD", 0.00001))
    big = buy(desk, "BTC-USD", 60_000.0)
    assert big.stop_price == pytest.approx(60_000 * 0.97) and big.take_profit == pytest.approx(60_000 * 1.06 + fees("BTC-USD", 60_000))
    assert big.avg_price == pytest.approx(60_000, rel=0.001)


def test_runner_banks_half_at_target_then_trails_in_profit(desk):
    desk.settings.skimmer = False  # the Portfolio Manager's runner on its own (Skimmer has its own tests)
    desk.settings.tp_step = False  # (the moving take-profit has its own tests)
    pos = buy(desk, "ETH-USD", 1000.0)
    q0, entry = pos.qty, pos.avg_price
    tick(desk, "ETH-USD", 1155.0)  # +15%: target
    assert pos.qty == pytest.approx(q0 * 0.5, rel=0.01)  # half banked
    assert pos.take_profit is None  # no ceiling on the runner
    assert pos.stop_price >= entry * 1.075  # stop moved into profit
    assert "ETH-USD" in desk.bots["portfolio"].scaled_out
    # a strategy "sell" doesn't cut the runner short
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.SELL, 0.9, "bearish", "quant"))
    run(desk.bus.drain())
    assert "holding the runner" in rejected[-1]["reason"] and pos.qty > 0
    # it keeps running: the stop trails the peak
    tick(desk, "ETH-USD", 1400.0)
    assert pos.qty > 0 and pos.stop_price >= 1400 - 0.08 * 1000 - 1
    trades = capture(desk, "trade.closed")
    tick(desk, "ETH-USD", pos.stop_price - 1)  # pulls back through the trailing stop
    assert pos.is_flat and trades[-1].pnl > 0 and trades[-1].exit_price > 1300


def test_runner_never_gives_back_the_locked_profit(desk):
    desk.settings.tp_step = False
    pos = buy(desk, "SOL-USD", 100.0)
    entry = pos.avg_price
    tick(desk, "SOL-USD", 115.5)
    trades = capture(desk, "trade.closed")
    for p in (113.0, 111.0, 109.0, 107.5):  # sells off back toward the entry
        tick(desk, "SOL-USD", p)
    assert pos.is_flat and trades[-1].pnl > 0 and trades[-1].exit_price > entry * 1.07  # profit kept
    assert desk.bots["portfolio"].metrics.get("runners") == 1


def test_meme_coins_trade_smaller_and_are_capped(desk):
    eq = desk.portfolio.equity
    meme = buy(desk, "PEPE-USD", 0.00001)
    assert meme.qty > 0
    # money at risk to the -8% stop is half a normal trade's risk (1.5%) x conviction (0.5 + 0.5 x 0.9)
    at_risk = meme.qty * 0.00001 * (0.08 + desk.settings.round_trip_cost("PEPE-USD", 1.0))
    assert at_risk == pytest.approx(eq * desk.settings.risk.risk_per_trade * 0.95 * desk.settings.meme_risk_mult, rel=0.05)
    buy(desk, "DOGE-USD", 0.2)
    buy(desk, "SHIB-USD", 0.00002)
    rejected = capture(desk, "signal.rejected")
    fourth = buy(desk, "BONK-USD", 0.00003)
    assert fourth.is_flat and "meme coins" in rejected[-1]["reason"]


def test_coins_the_real_money_broker_cant_trade_are_practice_only():
    from tests.test_live_switch import make_desk

    desk = make_desk(symbols=["BTC-USD", "BONK-USD"])
    run(desk.switch_mode("live"))
    rejected = capture(desk, "signal.rejected")
    assert buy(desk, "BONK-USD", 0.00003).is_flat
    assert "practice only" in rejected[-1]["reason"]
    assert Settings.live_tradeable("PEPE-USD") and Settings.live_tradeable("AAPL")


def _snap(desk, sym, price, high, fast, slow):
    ind = {"atr": price * 0.01, "volatility": 0.01, "rsi": 75, "ema_fast": fast, "ema_slow": slow,
           "sma50": price, "donchian_high": high, "donchian_low": price * 0.9}
    desk.bots["quant"].last_snap[sym] = Snapshot(sym, price, ind)
    desk.bots["risk"].snapshots[sym] = Snapshot(sym, price, ind)


def test_snipes_a_meme_breakout_on_the_tick(desk):
    sym = "PEPE-USD"
    desk.settings.symbols.append(sym)
    props = capture(desk, "signal.proposed")
    _snap(desk, sym, 1.00e-5, 1.00e-5, fast=1.02e-5, slow=1.00e-5)  # uptrend, sitting at the high
    tick(desk, sym, 1.005e-5)  # +0.5%: not a breakout yet
    assert not props
    tick(desk, sym, 1.02e-5)  # +2% through the high
    assert props and props[0].reason.startswith("snipe") and desk.portfolio.position(sym).qty > 0
    assert desk.bots["quant"].metrics["snipes"] == 1
    tick(desk, sym, 1.03e-5)  # same breakout: one shot only
    assert desk.bots["quant"].metrics["snipes"] == 1


def test_no_snipe_without_momentum_or_on_majors(desk):
    desk.settings.symbols += ["DOGE-USD"]
    props = capture(desk, "signal.proposed")
    _snap(desk, "DOGE-USD", 0.2, 0.2, fast=0.19, slow=0.2)  # spike against a downtrend
    tick(desk, "DOGE-USD", 0.21)
    _snap(desk, "BTC-USD", 50_000, 50_000, fast=51_000, slow=50_000)  # not a meme coin
    tick(desk, "BTC-USD", 52_000)
    assert not [p for p in props if p.reason.startswith("snipe")]


def test_coinbase_feed_skips_coins_it_doesnt_list():
    def handler(req):
        return httpx.Response(200, json=[{"id": "BTC-USD", "status": "online"}, {"id": "PEPE-USD", "status": "online"},
                                         {"id": "WIF-USD", "status": "delisted"}])
    syms = ["BTC-USD", "PEPE-USD", "WIF-USD", "NOPE-USD"]
    feed = CoinbaseFeed(syms, client=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)),
                        use_ws=False)
    run(feed.start())
    assert syms == ["BTC-USD", "PEPE-USD"] and feed.dropped == ["WIF-USD", "NOPE-USD"]

    def down(req):
        return httpx.Response(503)
    keep = ["BTC-USD", "BONK-USD"]
    run(CoinbaseFeed(keep, client=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(down)),
                     use_ws=False).start())
    assert keep == ["BTC-USD", "BONK-USD"]  # unknown -> keep everything


def test_flat_trade_in_profit_is_flipped_to_free_the_slot(desk):
    import time as _t
    pos = buy(desk, "SOL-USD", 100.0)
    tick(desk, "SOL-USD", 101.5)  # +1.5%: in profit after fees, target (+6%) far away
    pm = desk.bots["portfolio"]
    pm._free_stale_slots()
    assert pos.qty > 0  # too early
    pos.opened_ts = _t.time() - 25 * 3600  # a day later, still nowhere near the target
    pm._free_stale_slots()
    run(desk.bus.drain())
    assert pos.is_flat and pm.metrics["stale_flips"] == 1
    # a loser is never flipped: it keeps its stop
    los = buy(desk, "AVAX-USD", 30.0)
    tick(desk, "AVAX-USD", 29.7)
    los.opened_ts = _t.time() - 25 * 3600
    pm._free_stale_slots()
    run(desk.bus.drain())
    assert los.qty > 0


def test_minor_models_dont_veto_in_the_wrong_kind_of_market(desk):
    from tradezone.core.models import Regime
    q = desk.bots["quant"]
    # strong uptrend, RSI overbought: the dip-buying model says "no" but barely counts in a trend
    votes = q.consensus(1, {"momentum": 0.9, "breakout": 1.0, "mean_reversion": -0.6}, Regime.TRENDING)
    assert votes["mean_reversion"] == "abstain" and votes["momentum"] == "yes"
    # but where it matters (sideways market) its objection still blocks
    votes = q.consensus(1, {"momentum": 0.3, "breakout": 0.0, "mean_reversion": -0.6}, Regime.RANGING)
    assert votes["mean_reversion"] == "no"


def test_todays_hunt_counts_what_the_bots_did_and_why(desk):
    from tradezone.core.models import Snapshot as Snap
    ind = {"atr": 1.0, "volatility": 0.01, "rsi": 50, "ema_fast": 100, "ema_slow": 100, "sma50": 100,
           "donchian_high": 101, "donchian_low": 99}
    desk.bus.publish("market.snapshot", Snap("BTC-USD", 100.0, ind))
    desk.bus.publish("market.snapshot", Snap("BTC-USD", 100.0, ind, historical=True))  # warm-up: not a chance
    run(desk.bus.drain())
    buy(desk, "SOL-USD", 100.0)
    desk.settings.risk.max_open_positions = 1
    buy(desk, "ETH-USD", 1000.0)  # skipped: slots full
    h = desk.state()["hunt"]
    assert h["checks"] >= 1 and h["agreed"] == 2 and h["opened"] == 1
    assert any("max open positions" in x["reason"] for x in h["skipped"])
    assert not any(ch.isdigit() for x in h["skipped"] for ch in x["reason"])  # numbers stripped, so reasons group
