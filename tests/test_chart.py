"""The live profit/loss chart: P&L over time with every trade on it, for practice and real money."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from tests.helpers import FakeLiveBroker, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def make(journal=None):
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", symbols=["BTC-USD", "ETH-USD"])
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=journal or Journal(":memory:"),
                       live_broker=FakeLiveBroker(cash=1_000.0))


def record(desk, ts):
    desk.journal.record_equity(ts, desk.portfolio.equity, desk.mode, desk.lifetime_pnl())


def test_chart_follows_pnl_and_marks_every_trade():
    desk = make()
    prime(desk, "BTC-USD", 50_000.0, atr=250.0)
    t = time.time()
    record(desk, t - 60)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("BTC-USD").qty > 0
    desk.portfolio.mark("BTC-USD", 51_000.0)
    record(desk, t - 30)
    c = desk.chart("paper", hours=1)
    assert c["mode"] == "paper" and c["current"] and len(c["points"]) >= 3  # 2 recorded + "now"
    assert c["points"][-1][1] == pytest.approx(desk.lifetime_pnl()) and c["pnl"] > 0
    assert [x["action"] for x in c["trades"]] == ["open"] and c["trades"][0]["symbol"] == "BTC-USD"
    assert c["fees"] > 0  # fees are shown with the chart


def test_practice_and_real_money_have_separate_lines_and_real_money_continues_across_sessions():
    desk = make()
    prime(desk, "BTC-USD", 50_000.0, atr=250.0)
    t = time.time()
    desk.portfolio.cash += 250.0  # practice made $250
    record(desk, t - 100)
    run(desk.switch_mode("live"))
    assert desk.lifetime_pnl() == pytest.approx(0.0)  # real money starts at zero, not at practice's +$250
    desk.portfolio.cash += 40.0  # real money made $40 this session
    record(desk, t - 50)
    run(desk.switch_mode("paper"))
    assert desk.lifetime_pnl() == pytest.approx(250.0)  # back to practice: its own line
    run(desk.switch_mode("live"))  # a new real-money session (a new book from the account's cash)
    assert desk.lifetime_pnl() == pytest.approx(40.0)  # ...carries on from +$40 instead of dropping to zero
    live, paper = desk.chart("live", hours=0), desk.chart("paper", hours=0)
    assert live["current"] and not paper["current"]
    assert all(abs(v - 40.0) < 1e-6 or abs(v) < 1e-6 for _, v in live["points"])
    assert [v for _, v in paper["points"]] == pytest.approx([250.0])


def test_old_journals_are_upgraded_and_old_unlabelled_points_are_left_out(tmp_path):
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE equity (ts REAL NOT NULL, equity REAL NOT NULL)")
    con.execute("INSERT INTO equity VALUES (?, ?)", (time.time() - 10, 123.0))  # from before: practice or real? unknown
    con.commit()
    con.close()
    j = Journal(db)
    j.record_equity(time.time(), 100.0, "paper", 5.0)
    assert [p[1] for p in j.pnl_series("paper", 0, time.time() + 1)] == [5.0]
    assert j.pnl_series("live", 0, time.time() + 1) == []


def test_long_ranges_are_thinned_for_the_phone():
    j = Journal(":memory:")
    t0 = time.time() - 7 * 86400
    for i in range(5_000):
        j.record_equity(t0 + i * 120, 100.0, "paper", float(i))
    pts = j.pnl_series("paper", t0, time.time(), points=400)
    assert 300 <= len(pts) <= 410 and pts[-1][1] == 4999.0  # the latest point is always kept


def test_chart_api():
    desk = make()
    client = TestClient(create_app(desk))
    r = client.get("/api/chart?mode=paper&hours=24")
    assert r.status_code == 200 and r.json()["mode"] == "paper"
    assert client.get("/api/chart?hours=-1").status_code == 422
    assert "pnl_point" in desk.state()
