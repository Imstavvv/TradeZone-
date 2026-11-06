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


def test_off_by_default_and_not_part_of_the_tested_best_settings():
    d = make()
    assert d.settings.falling_guard == "off" and "falling_guard" not in d.BEST


def test_switched_off_it_never_blocks_and_the_choice_is_saved():
    d = make(falling_guard="coin")
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


def test_switching_it_on_works_at_once_on_the_history_already_seen():
    d = make()  # off while the prices come in
    feed(d, "SOL-USD", falling(150.0))
    d.set_falling_guard(True)
    assert "downtrend" in (idea(d, "SOL-USD") or "")


def test_the_switch_works_from_the_app_and_survives_a_restart(tmp_path):
    from fastapi.testclient import TestClient

    from tradezone.server import create_app
    db = str(tmp_path / "tz.db")
    s = Settings(feed="sim", symbols=list(SYMS), starting_cash=1_000.0)
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    with TestClient(create_app(d, run_desk=False)) as c:
        assert c.post("/api/falling-guard", json={"on": True}).json()["falling_guard"] == "coin"
        assert c.get("/api/state").json()["falling_guard"] == "coin"
    s2 = Settings(feed="sim", symbols=list(SYMS), starting_cash=1_000.0)
    d2 = TradingDesk(s2, feed=SimulatedFeed(s2.symbols), journal=Journal(db))
    run(d2.start())
    assert d2.settings.falling_guard == "coin"
    run(d2.shutdown())


def test_the_underwater_budget_shrinks_a_new_buy_to_fit():
    d = make(underwater_budget=0.20)
    feed(d, "SOL-USD", [150.0] * 5)  # (indicators for the sizing)
    approved = capture(d, "order.approved")
    rej = capture(d, "signal.rejected")
    d.bots["risk"].evaluate(Signal("SOL-USD", Side.BUY, 0.99, "test", "quant", kind=SignalKind.ENTRY))
    run(d.bus.drain())
    assert approved, rej
    assert approved[-1].qty * d.prices["SOL-USD"] <= 0.20 * d.portfolio.equity + 1e-6


def test_trend_split_is_off_by_default():
    d = make()
    assert d.settings.trend_split_falling_size == 1.0 and d.settings.trend_split_rising_past_cap is False


def test_trend_split_reads_each_coins_own_trend():
    d = make()
    feed(d, "SOL-USD", falling(150.0))
    feed(d, "ETH-USD", falling(3_000.0, step=0.001))
    feed(d, "BTC-USD", falling(60_000.0, n=50))
    r = d.bots["risk"]
    assert r._trend("SOL-USD") == "down" and r._trend("ETH-USD") == "up" and r._trend("BTC-USD") is None


def test_trend_split_can_keep_out_of_a_falling_coin():
    d = make(trend_split_falling_size=0.0)
    feed(d, "SOL-USD", falling(150.0))
    assert "clear downtrend" in (idea(d, "SOL-USD") or "")


def test_a_rising_coin_may_pass_a_full_underwater_cap():
    d = make(trend_split_rising_past_cap=True)
    d.settings.loser_cap = 0.15
    d.portfolio.positions["BTC-USD"] = Position(symbol="BTC-USD", qty=0.005, avg_price=60_000.0, last_price=50_000.0)
    d.portfolio.cash -= 300.0
    d.prices["BTC-USD"] = 50_000.0
    feed(d, "SOL-USD", falling(150.0))
    assert "underwater cap" in (idea(d, "SOL-USD") or "")  # a falling coin still waits
    feed(d, "ETH-USD", falling(3_000.0, step=0.001))
    assert "underwater cap" not in (idea(d, "ETH-USD") or "")  # a rising one goes
