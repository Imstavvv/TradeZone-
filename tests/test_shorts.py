"""Shorting: profit from falling prices with the same stop/target percentages, mirrored."""

import pytest

from tests.helpers import capture, prime, run
from tradezone.core.models import Side, Signal, Snapshot, Tick


def short(desk, sym, price):
    if sym not in desk.settings.symbols:
        desk.settings.symbols.append(sym)
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.SELL, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def test_short_uses_the_same_percentages_mirrored(desk):
    big = short(desk, "BTC-USD", 50_000.0)
    assert big.qty < 0
    assert big.stop_price == pytest.approx(50_000 * 1.03) and big.take_profit == pytest.approx(50_000 * 0.94)
    meme = short(desk, "DOGE-USD", 0.2)
    assert meme.stop_price == pytest.approx(0.2 * 1.08) and meme.take_profit == pytest.approx(0.2 * 0.85)


def test_short_profits_when_the_price_falls_and_stops_out_when_it_rises(desk):
    desk.settings.skimmer = False  # just the plan itself
    pos = short(desk, "ETH-USD", 1000.0)
    trades = capture(desk, "trade.closed")
    tick(desk, "ETH-USD", 935.0)  # -6.5%: target hit, half banked, the rest runs
    assert trades[-1].pnl > 0 and pos.qty < 0
    tick(desk, "ETH-USD", 990.0)  # bounce: the runner's stop (in profit) closes it
    assert pos.is_flat and trades[-1].pnl > 0
    los = short(desk, "SOL-USD", 100.0)
    tick(desk, "SOL-USD", 103.5)  # +3.5% against us: stop
    assert los.is_flat and trades[-1].pnl < 0


def test_skimmer_banks_profit_on_shorts_too(desk):
    pos = short(desk, "LINK-USD", 20.0)
    q0 = pos.qty
    tick(desk, "LINK-USD", 19.35)  # -3.25%: more than halfway to the -6% target
    assert pos.qty == pytest.approx(q0 * 0.75, rel=0.01)


def test_meme_breakdown_is_sniped_as_a_short(desk):
    sym = "PEPE-USD"
    desk.settings.symbols.append(sym)
    ind = {"atr": 1e-7, "volatility": 0.01, "rsi": 30, "ema_fast": 0.98e-5, "ema_slow": 1.0e-5,
           "sma50": 1e-5, "donchian_high": 1.05e-5, "donchian_low": 1.0e-5}
    desk.bots["quant"].last_snap[sym] = desk.bots["risk"].snapshots[sym] = Snapshot(sym, 1e-5, ind)
    tick(desk, sym, 0.98e-5)  # -2% through the recent low, momentum down
    assert desk.portfolio.position(sym).qty < 0


def test_no_crypto_shorts_with_real_money():
    from tests.test_live_switch import make_desk
    desk = make_desk()
    run(desk.switch_mode("live"))
    rejected = capture(desk, "signal.rejected")
    assert short(desk, "BTC-USD", 50_000.0).is_flat
    assert "can't short crypto" in rejected[-1]["reason"]


def test_short_sale_cash_is_not_free_to_withdraw(desk):
    cash0 = desk.portfolio.cash
    short(desk, "ETH-USD", 1000.0)
    assert desk.portfolio.cash > cash0  # the sale put cash in the account...
    assert desk.wallets.free_cash() == pytest.approx(cash0, rel=0.01)  # ...but it's owed back
