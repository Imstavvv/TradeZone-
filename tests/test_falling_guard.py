"""The falling-market guard: no new buys while a coin (or most of the market) is in a downtrend.
Exits, stops and your own buys are never blocked by it."""

from tests.helpers import capture, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, SignalKind, Snapshot
from tradezone.core.portfolio import Position
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

SYMS = ["BTC-USD", "ETH-USD", "SOL-USD"]


def make(**kw):
    s = Settings(feed="sim", symbols=list(SYMS), starting_cash=1_000.0, loser_cap=0.0, **kw)
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    d.prices.update({"BTC-USD": 60_000.0, "ETH-USD": 3_000.0, "SOL-USD": 150.0})
    return d


def feed(desk, sym, prices):
    risk = desk.bots["risk"]
    for p in prices:
        risk.on_snapshot(type("E", (), {"payload": Snapshot(sym, p, {"atr": p * 0.01, "volatility": 0.01, "rsi": 50.0, "ema_fast": p, "ema_slow": p, "sma50": p})})())
    desk.prices[sym] = prices[-1]


def falling(start, n=300, step=-0.001):
    return [start * (1 + step) ** i for i in range(n)]


def idea(desk, sym, side=Side.BUY, force=False):
    out = capture(desk, "signal.rejected")
    desk.bots["risk"].evaluate(Signal(sym, side, 0.99, "test", "quant", kind=SignalKind.ENTRY, force=force))
    run(desk.bus.drain())
    return out[-1]["reason"] if out else None


def test_on_by_default_as_part_of_the_tested_best_settings():
    d = make()
    assert d.settings.falling_guard == "coin" and d.BEST["falling_guard"] == "coin"
    assert "falling_guard" not in [x["key"] for x in d.protocol_status()["off"]]


def test_switched_off_it_never_blocks_and_the_choice_is_saved():
    d = make()
    d.set_falling_guard(False)
    assert d.settings.falling_guard == "off" and d.store.load("falling_guard") == {"mode": "off"}
    feed(d, "SOL-USD", falling(150.0))
    assert "downtrend" not in (idea(d, "SOL-USD") or "")
    d.set_falling_guard(True)
    assert d.settings.falling_guard == "coin"


def test_coin_mode_blocks_buys_in_a_downtrend_and_allows_them_in_an_uptrend():
    d = make(falling_guard="coin")
    feed(d, "SOL-USD", falling(150.0))
    assert "downtrend" in (idea(d, "SOL-USD") or "")
    feed(d, "ETH-USD", falling(3_000.0, step=0.001))
    assert "downtrend" not in (idea(d, "ETH-USD") or "")


def test_too_little_history_never_blocks():
    d = make(falling_guard="coin")
    feed(d, "SOL-USD", falling(150.0, n=200))  # under the full 4 hours
    assert "downtrend" not in (idea(d, "SOL-USD") or "")


def test_selling_what_you_hold_and_your_own_buys_are_never_blocked():
    d = make(falling_guard="coin", never_sell_at_loss=False)
    feed(d, "SOL-USD", falling(150.0))
    assert "downtrend" not in (idea(d, "SOL-USD", force=True) or "")
    px = d.prices["SOL-USD"]
    d.portfolio.positions["SOL-USD"] = Position(symbol="SOL-USD", qty=1.0, avg_price=px, last_price=px)
    approved = capture(d, "order.approved")
    d.bots["risk"].evaluate(Signal("SOL-USD", Side.SELL, 0.99, "stop", "portfolio", kind=SignalKind.EXIT))
    run(d.bus.drain())
    assert approved


def test_market_mode_waits_for_most_coins_to_fall():
    d = make(falling_guard="market")
    feed(d, "BTC-USD", falling(60_000.0))
    feed(d, "ETH-USD", falling(3_000.0, step=0.001))
    feed(d, "SOL-USD", falling(150.0, step=0.001))
    assert "market is falling" not in (idea(d, "SOL-USD") or "")  # 1 of 3 falling
    feed(d, "ETH-USD", falling(3_000.0))
    assert "market is falling" in (idea(d, "SOL-USD") or "")  # 2 of 3
