"""The goal is gone: no daily goal and no good-day slowdown - the bots take their calculated risks every day.
The real risk controls stay (stops, the loss floor, the underwater cap, never-sell, the losing-day brake)."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def make(cash=100_000.0, journal=None):
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD"], starting_cash=cash)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=journal or Journal(":memory:"))


def buy(desk, sym="BTC-USD"):
    prime(desk, sym, 50_000.0, atr=250.0)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym), rejected


def test_there_is_no_daily_goal_any_more():
    desk = make()
    assert desk.settings.daily_goal_usd == 0.0 and desk.daily_goal() == 0.0
    desk.goal["daily"] = 1_000.0  # (a daily goal saved before the change)
    assert desk.daily_goal() == 0.0  # no longer changes how the bots trade
    assert desk.bots["risk"].day_guard()["goal_day"] is False


def test_a_big_day_keeps_trading_at_full_size():
    normal = buy(make(), "ETH-USD")[0].qty
    desk = make()
    risk = desk.bots["risk"]
    desk.portfolio.cash += 3_000.0  # a great day...
    risk._track_day_peak()
    desk.portfolio.cash -= 2_000.0  # ...then most of it given back
    pos, _ = buy(desk, "ETH-USD")
    assert pos.qty == pytest.approx(normal, rel=0.05)  # no slowdown: full size


def test_a_losing_day_still_brakes():
    normal = buy(make(), "ETH-USD")[0].qty
    desk = make()
    lim = desk.settings.limits(False).max_daily_loss_pct
    desk.portfolio.cash -= desk.portfolio.equity * lim * 0.6  # down more than half the daily limit
    assert desk.bots["risk"].day_guard()["brake"]
    pos, _ = buy(desk, "ETH-USD")
    assert 0 < pos.qty < normal * 0.6  # (the brake is loss protection, not a goal: it stays)


def test_the_goal_setting_endpoint_still_answers():
    with TestClient(create_app(make(), run_desk=False)) as c:
        assert c.get("/api/health").status_code == 200
