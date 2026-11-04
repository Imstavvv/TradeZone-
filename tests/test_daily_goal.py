"""The daily goal: a $1,000+ day with the money on hand - tracked, protected once reached, never forced."""

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


def test_progress_toward_a_1000_dollar_day():
    desk = make()
    d = desk.daily_goal_status()
    assert d["goal"] == 1_000 and d["today"] == 0 and not d["reached"] and d["realistic"]  # 1% of $100k
    desk.portfolio.cash += 400.0
    assert desk.daily_goal_status()["progress"] == pytest.approx(0.4)
    assert desk.state()["goal"]["daily"]["today"] == pytest.approx(400.0)


def test_a_small_balance_is_told_plainly_and_risk_is_never_raised_for_it():
    small = make(cash=50.0)
    d = small.daily_goal_status()
    assert not d["realistic"] and d["pct_of_balance"] == pytest.approx(20.0)  # 2,000% of $50 in a day
    off = make(cash=50.0)
    off.set_daily_goal(0)
    for d in (small, off):
        d.settings.focus_small_coins = False  # (compare like for like)
    a, _ = buy(small)
    b, _ = buy(off)
    assert a.qty > 0 and a.qty == pytest.approx(b.qty)  # the goal never makes a trade bigger


def test_a_goal_day_is_protected():
    desk = make()
    risk = desk.bots["risk"]
    desk.portfolio.cash += 1_200.0  # a $1,200 day (only +1.2%: below the usual 2% day-lock trigger)
    risk._track_day_peak()
    assert risk.day_guard()["goal_day"] and not risk.day_guard()["locked"]
    assert buy(desk, "ETH-USD")[0].qty > 0  # still trading while it holds
    desk.portfolio.positions.pop("ETH-USD")
    desk.portfolio.cash -= 350.0  # gave back over a quarter of it
    g = risk.day_guard()
    assert g["locked"]
    pos, rejected = buy(desk)
    assert pos.is_flat and "locking in today's gains" in rejected[-1]["reason"]


def test_the_daily_goal_can_be_changed_and_is_remembered(tmp_path):
    db = str(tmp_path / "tz.db")
    desk = make(journal=Journal(db))
    c = TestClient(create_app(desk))
    assert c.post("/api/goal", json={"start": 50, "target": 10_000, "daily": 250}).status_code == 200
    assert desk.daily_goal() == 250
    desk.set_goal(100, 5_000)
    assert desk.daily_goal() == 250  # changing the big goal keeps the daily one
    desk.save_state()
    again = make(journal=Journal(db))
    run(again.restore_state())
    assert again.daily_goal() == 250
    with pytest.raises(ValueError):
        desk.set_daily_goal(-5)
