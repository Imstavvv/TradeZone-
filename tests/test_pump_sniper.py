"""The Pump Sniper (asked for): in as a pump starts, out fast."""

import time

import pytest

from tests.helpers import capture, prime, run
from tests.test_new_bots import make, snap
from tradezone.core.models import Side, Signal


def pump(desk, sym="DOGE-USD", start=0.2, step=0.012, vr5=4.0, t0=None):
    t0 = t0 or time.time()
    p = start
    for i in range(4):
        snap(desk, sym, p, ts=t0 + 60 * i, volume_ratio_5=vr5, donchian_high=p * 0.999, rsi=70)
        p *= 1 + step
    return p / (1 + step)


def test_on_by_default_and_it_snipes_a_pump_at_half_size_aiming_for_4_percent_after_fees():
    desk = make(pump_sniper=False)
    assert make().settings.pump_sniper is True  # (on: asked for)
    props = capture(desk, "signal.proposed")
    pump(desk)
    assert not [p for p in props if p.source == "pumpsniper"]
    desk = make(pump_sniper=True)
    prime(desk, "DOGE-USD", 0.2, atr=0.001)
    props = capture(desk, "signal.proposed")
    pump(desk)
    run(desk.bus.drain())
    snipes = [p for p in props if p.source == "pumpsniper"]
    assert len(snipes) == 1 and "pump snipe" in snipes[0].reason
    pos = desk.portfolio.position("DOGE-USD")
    assert pos.qty > 0
    cost = desk.settings.round_trip_cost("DOGE-USD", pos.avg_price)
    assert pos.take_profit == pytest.approx(pos.avg_price * 1.04 + cost, rel=0.01)


def test_no_snipe_without_the_volume_or_when_whales_sell_or_on_bitcoin():
    for kw, sym in (({"vr5": 1.5}, "DOGE-USD"), ({}, "BTC-USD")):
        desk = make(pump_sniper=True)
        props = capture(desk, "signal.proposed")
        pump(desk, sym, start=0.2 if sym != "BTC-USD" else 60_000, **kw)
        assert not [p for p in props if p.source == "pumpsniper"]
    desk = make(pump_sniper=True)
    desk.bots["smartmoney"].tape["DOGE-USD"] = {"lean": -0.9, "buy_usd": 1e3, "sell_usd": 5e4, "at": time.time()}
    props = capture(desk, "signal.proposed")
    pump(desk)
    assert not [p for p in props if p.source == "pumpsniper"]


def test_a_stalled_pump_in_profit_is_sold_after_45_minutes_of_market_time():
    desk = make(pump_sniper=True)
    prime(desk, "DOGE-USD", 0.2, atr=0.001)
    t0 = time.time()
    px = pump(desk, t0=t0)
    run(desk.bus.drain())
    bot = desk.bots["pumpsniper"]
    assert desk.portfolio.position("DOGE-USD").qty > 0
    exits = capture(desk, "signal.proposed")
    up = px * 1.03
    desk.prices["DOGE-USD"] = up
    desk.portfolio.mark("DOGE-USD", up)
    snap(desk, "DOGE-USD", up, ts=t0 + 60 * 20)

    async def cycle():
        await bot.on_cycle()
        await desk.bus.drain()
    run(cycle())
    assert not [e for e in exits if "stalled" in e.reason]  # (20 min: not yet)
    snap(desk, "DOGE-USD", up, ts=t0 + 60 * 50)
    run(cycle())
    assert [e for e in exits if "stalled" in e.reason]


def test_a_turned_down_snipe_never_times_out_another_strategys_trade():
    desk = make(pump_sniper=True)
    prime(desk, "DOGE-USD", 0.2, atr=0.001)
    desk.bots["risk"].enabled = False  # (the snipe goes nowhere)
    t0 = time.time()
    pump(desk, t0=t0)
    run(desk.bus.drain())
    bot = desk.bots["pumpsniper"]
    assert desk.portfolio.position("DOGE-USD").qty == 0 and not bot.opened
    desk.bots["risk"].enabled = True
    desk.bus.publish("signal.proposed", Signal("DOGE-USD", Side.BUY, 0.95, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("DOGE-USD").qty > 0 and not bot.opened  # (another strategy's trade: not timed)
