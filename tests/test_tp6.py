"""Take-profit +6% after every fee (replayed against +4% ... +25%: the only one to beat +15% on both halves)."""

import pytest

from tests.helpers import prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def test_the_plan_is_minus_7_plus_6_after_fees():
    s = Settings()
    assert s.plan_pcts("BTC-USD")[1] == 0.06 and s.plan_pcts("DOGE-USD") == (0.07, 0.06)  # (the -7% wide stop on top)
    assert s.tp_step_pct == 0.06 and s.max_hold_hours == 6 and s.meme_max_hold_hours == 3


def test_a_new_trade_aims_for_6_percent_after_both_fees():
    s = Settings(feed="sim", symbols=["BTC-USD"])
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    prime(d, "BTC-USD", 50_000.0, atr=250.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    pos = d.portfolio.position("BTC-USD")
    assert pos.qty > 0
    cost = s.round_trip_cost("BTC-USD", pos.avg_price)
    assert pos.take_profit == pytest.approx(pos.avg_price * 1.06 + cost, rel=0.01)
    assert pos.take_profit > pos.avg_price * 1.06  # (every fee covered on top)


def test_open_trades_on_the_old_automatic_15_move_to_6_once_a_hand_set_target_stays(tmp_path):
    db = str(tmp_path / "t.db")
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD"])
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    for sym, px in (("BTC-USD", 50_000.0), ("ETH-USD", 3_000.0)):
        prime(d, sym, px, atr=px * 0.005)
        d.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.95, "t", "quant"))
        run(d.bus.drain())
    btc, eth = d.portfolio.position("BTC-USD"), d.portfolio.position("ETH-USD")
    btc.take_profit = btc.avg_price * 1.15 + s.round_trip_cost("BTC-USD", btc.avg_price)  # (the old automatic one)
    eth.take_profit = eth.avg_price * 1.30  # (set by hand)
    d._tp6_once()
    assert btc.take_profit == pytest.approx(btc.avg_price * 1.06 + s.round_trip_cost("BTC-USD", btc.avg_price))
    assert eth.take_profit == pytest.approx(eth.avg_price * 1.30)
    btc.take_profit = 1.0
    d._tp6_once()  # (once only)
    assert btc.take_profit == 1.0


def test_a_buy_needs_conviction_half_and_three_families():
    s = Settings()
    assert s.risk.min_signal_strength == 0.5 and s.risk.consensus_min_yes == 3


def test_runners_are_back_on_with_the_6_percent_plan():
    assert Settings().runners is True  # (replayed with +6%: +$28.28 training / +$74.89 unseen)


def test_what_hurt_profit_is_off():
    s = Settings()
    assert s.falling_buys == "block" and not s.pullback and not s.snipe and s.pump_sniper
