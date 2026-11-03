"""Skimmer: quick day-trade skims and profit-taking of every size, never at a loss."""

import time

import pytest

from tests.helpers import capture, prime, run
from tradezone.core.models import Side, Signal, Snapshot, Tick


def ind(price, **kw):
    d = {"atr": price * 0.004, "volatility": 0.0015, "rsi": 40, "ema_fast": price * 1.01, "ema_slow": price * 0.99,
         "sma50": price * 0.98, "donchian_high": price * 1.03, "donchian_low": price * 0.95}
    d.update(kw)
    return d


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def skim(desk, sym="SOL-USD", price=100.0, **kw):
    """Offer the Skimmer a dip-in-an-uptrend bar (Quant switched off so only the Skimmer acts)."""
    desk.set_bot_enabled("quant", False)
    if sym not in desk.settings.symbols:
        desk.settings.symbols.append(sym)
    prime(desk, sym, price, atr=price * 0.004)
    snap = Snapshot(sym, price, ind(price, **kw))
    desk.bots["risk"].snapshots[sym] = snap
    desk.bus.publish("market.snapshot", snap)
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def test_skims_a_dip_in_an_uptrend_with_a_small_target_and_tight_stop(desk):
    pos = skim(desk)
    assert pos.qty > 0 and desk.bots["skimmer"].owns("SOL-USD")
    assert pos.stop_price == pytest.approx(100 * (1 - desk.settings.skim_stop_pct))
    assert pos.take_profit == pytest.approx(100 * (1 + desk.settings.skim_target_pct))
    # at the small target the whole skim is sold (no runner for a skim)
    trades = capture(desk, "trade.closed")
    tick(desk, "SOL-USD", 102.6)  # +2.5% target
    assert pos.is_flat and trades[-1].pnl > 0
    assert not desk.bots["skimmer"].owns("SOL-USD")
    assert desk.bots["skimmer"].metrics["skim_wins"] == 1


def test_no_skim_without_trend_and_dip_or_on_meme_coins(desk):
    assert skim(desk, rsi=60).is_flat  # no dip
    assert skim(desk, "ETH-USD", 1000.0, ema_fast=990.0, ema_slow=1000.0).is_flat  # no uptrend
    assert skim(desk, "PEPE-USD", 0.00001).is_flat  # meme coins are for snipes, not skims
    assert skim(desk, "AVAX-USD", 30.0, volatility=0.0001).is_flat  # too calm to reach +2% in time
    assert skim(desk, "LINK-USD", 20.0, volatility=0.004).is_flat  # too wild for a tight stop


def test_skim_time_limit_banks_profit_but_never_sells_a_loser(desk):
    sk = desk.bots["skimmer"]
    pos = skim(desk)
    tick(desk, "SOL-USD", 101.0)  # in profit after fees, target not reached
    sk.owned["SOL-USD"] = time.time() - 3 * 3600  # time's up
    run(sk.on_cycle())
    run(desk.bus.drain())
    assert pos.is_flat and sk.metrics["time_exits"] == 1
    los = skim(desk, "LINK-USD", 20.0)
    tick(desk, "LINK-USD", 19.9)
    sk.owned["LINK-USD"] = time.time() - 3 * 3600
    run(sk.on_cycle())
    run(desk.bus.drain())
    assert los.qty > 0  # still protected by its stop, not dumped by the clock


def buy(desk, sym, price):
    if sym not in desk.settings.symbols:
        desk.settings.symbols.append(sym)
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def test_banks_a_slice_halfway_to_the_target(desk):
    pos = buy(desk, "BTC-USD", 50_000.0)  # -3% / +6% plan
    q0 = pos.qty
    tick(desk, "BTC-USD", 51_000.0)  # +2%: not halfway yet
    assert pos.qty == q0
    tick(desk, "BTC-USD", 51_600.0)  # +3.2%: halfway
    assert pos.qty == pytest.approx(q0 * 0.75, rel=0.01)
    tick(desk, "BTC-USD", 51_700.0)  # once per trade
    assert pos.qty == pytest.approx(q0 * 0.75, rel=0.01)
    assert desk.bots["skimmer"].metrics["banked_small"] == 1


def test_sells_half_into_a_sharp_spike(desk):
    pos = buy(desk, "ETH-USD", 1000.0)
    q0 = pos.qty
    for px in (1000.0, 1001.0):  # recent 1-minute bars
        desk.bus.publish("market.snapshot", Snapshot("ETH-USD", px, ind(px, rsi=55)))
    run(desk.bus.drain())
    tick(desk, "ETH-USD", 1027.0)  # +2.6% above the recent bars
    assert pos.qty == pytest.approx(q0 * 0.5, rel=0.01)
    assert desk.bots["skimmer"].metrics["banked_spikes"] == 1


def test_never_takes_profit_that_isnt_there(desk):
    pos = buy(desk, "ETH-USD", 1000.0)
    q0 = pos.qty
    tick(desk, "ETH-USD", 975.0)  # down
    tick(desk, "ETH-USD", 1003.0)  # a +2.9% bounce off the low, but the trade is only +0.3% (fees ~0.6%)
    assert pos.qty == q0
    desk.settings.skimmer = False
    tick(desk, "ETH-USD", 1035.0)
    assert pos.qty == q0  # switched off: nothing
