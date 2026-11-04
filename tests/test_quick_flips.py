"""Real money: quick flips - more trades, smaller wins banked sooner, every one counted after fees."""

import time

import pytest

from tests.helpers import FakeLiveBroker, capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Fill, Side, Signal, Tick
from tradezone.core.portfolio import Portfolio
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


class FeeLiveBroker(FakeLiveBroker):
    """A live account that charges a real fee (0.40% per side, like Kraken)."""

    def __init__(self, cash=5_000.0):
        super().__init__(cash)
        self.fee = lambda _s: 40.0


def make(quick=True, **kw):
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", crypto_fee_bps=40.0,
                 symbols=["BTC-USD", "ETH-USD", "PEPE-USD"], live_quick_flips=quick, **kw)
    s.risk.symbol_cooldown_s = 0
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=FeeLiveBroker())
    prime(desk, "BTC-USD", 50_000.0, atr=250.0)
    run(desk.switch_mode("live"))
    return desk


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def buy(desk, sym="BTC-USD", price=50_000.0, strength=0.9):
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, strength, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def test_live_uses_the_quick_flip_plan_with_targets_after_fees():
    desk = make()
    pos = buy(desk)
    fees = desk.settings.round_trip_cost("BTC-USD", 50_000.0)
    assert pos.qty > 0 and desk.bots["risk"].quick
    assert pos.stop_price == pytest.approx(50_000 * 0.98, rel=1e-3)  # -2% quick-flip stop
    assert pos.take_profit == pytest.approx(50_000 * 1.04 + fees, rel=1e-3)  # +4% AFTER both fees
    practice = make(quick=False)
    assert not practice.bots["risk"].quick
    assert buy(practice).take_profit > pos.take_profit  # off: the normal, wider +6% plan


def test_live_takes_trades_with_a_little_less_conviction():
    desk = make()
    assert buy(desk, strength=0.32).qty > 0  # practice needs 0.35
    calm = make(quick=False)
    assert buy(calm, strength=0.32).is_flat


def test_quick_flip_banks_half_once_the_profit_after_fees_is_real():
    desk = make()
    pos = buy(desk)
    q0 = pos.qty
    tick(desk, "BTC-USD", 50_500.0)  # +1% move, but fees eat most of it: hold
    assert pos.qty == q0
    trades = capture(desk, "trade.closed")
    tick(desk, "BTC-USD", 51_100.0)  # +2.2% move = about +1.3% after both fees: bank half
    assert pos.qty == pytest.approx(q0 / 2, rel=0.01)
    assert desk.bots["skimmer"].metrics["quick_flips"] == 1
    t = trades[-1]
    sold = t.qty
    gross = (t.exit_price - t.entry_price) * sold
    sell_fee = t.exit_price * sold * 0.004
    buy_fee = t.entry_price * sold * 0.004
    assert t.pnl == pytest.approx(gross - sell_fee - buy_fee, rel=1e-6) and t.pnl > 0  # both fees in the result


def test_a_stale_trade_is_closed_sooner_but_only_in_profit_after_fees():
    desk = make()
    pm = desk.bots["portfolio"]
    pos = buy(desk)
    pos.opened_ts = time.time() - 3.5 * 3600  # past the 3 h quick-flip hold (practice: 24 h)
    tick(desk, "BTC-USD", 50_300.0)  # +0.6%: less than the 0.8% round trip -> a loss after fees
    pm._free_stale_slots()
    run(desk.bus.drain())
    assert pos.qty > 0  # never closed at a loss by the clock
    tick(desk, "BTC-USD", 50_700.0)  # +1.4%: a small real profit after fees
    pm._free_stale_slots()
    run(desk.bus.drain())
    assert pos.is_flat and pm.metrics["stale_flips"] == 1


def test_fees_are_always_counted_and_hard_limits_unchanged():
    p = Portfolio(1_000)
    p.apply_fill(Fill("a", "X", Side.BUY, 10, 10.0, 0.4))  # buy fee 0.40
    half = p.apply_fill(Fill("b", "X", Side.SELL, 5, 11.0, 0.22))
    assert half.pnl == pytest.approx(5 * 1.0 - 0.22 - 0.2)  # sell fee + half the buy fee
    rest = p.apply_fill(Fill("c", "X", Side.SELL, 5, 11.0, 0.22))
    assert rest.pnl == pytest.approx(5 * 1.0 - 0.22 - 0.2)
    assert half.pnl + rest.pnl == pytest.approx(p.cash - 1_000)  # the trades add up to the cash, to the cent
    s = Settings()
    assert (s.live_max_daily_loss_pct, s.live_max_drawdown_pct, s.live_capital_floor_pct) == (0.03, 0.10, 0.10)
    s2 = Settings(kraken_key="k", kraken_secret="s", feed="coinbase")
    s2.apply_venue_costs()
    assert s2.venue() == "kraken" and s2.crypto_fee_bps == 40  # practice charges Kraken's real fee
