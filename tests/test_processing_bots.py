"""Processing helpers: the Speed Monitor times the team; the Housekeeper keeps the records lean."""

import json
import time

from tests.helpers import capture, run
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(db=":memory:"):
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD", "SOL-USD"])
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))


def test_speed_monitor_times_every_cycle_and_names_the_busiest_bots():
    desk = make()
    run(desk.start())
    for _ in range(61):
        run(desk.step())
    m = desk.bots["speed"].metrics
    assert m["cycle_ms"] >= 0 and m["avg_cycle_ms"] < 1000 and m["load_pct"] < 50
    assert m["busiest"] and "ms" in m["busiest"]


def test_speed_monitor_warns_when_the_bots_fall_behind():
    desk = make()
    sp = desk.bots["speed"]
    alerts = capture(desk, "alert")
    for _ in range(60):
        desk.last_cycle_s = desk.settings.cycle_seconds * 0.9  # 90% of the time between cycles
        run(sp.on_cycle())
    run(desk.bus.drain())
    assert any("falling behind" in a.message for a in alerts)


def test_housekeeper_clears_old_working_notes_but_keeps_trades_and_money_records(tmp_path):
    desk = make(str(tmp_path / "tz.db"))
    j, t = desk.journal, time.time()
    old, recent = t - 5 * 86_400, t - 3600
    for topic in ("signal.proposed", "signal.rejected", "regime.update", "trade.log", "stack.buy", "funds.deposit",
                  "desk.control", "order.update", "alert"):
        for ts in (old, recent):
            j.record(ts, topic, "test", {"x": 1}, "paper")
    j.record(t - 40 * 86_400, "alert", "test", {"x": 1}, "paper")  # a 40-day-old alert
    for i in range(600):  # old chart points: one a second for 10 minutes, 3 days ago
        j.record_equity(t - 3 * 86_400 + i, 1000.0 + i, "paper", float(i))
    j.commit()
    hk = desk.bots["housekeeper"]
    hk.last_compact -= 2 * 86_400
    res = hk.tidy()
    left = {r[0]: r[1] for r in j.all("SELECT topic, COUNT(*) FROM events GROUP BY topic")}
    for topic in ("signal.proposed", "signal.rejected", "regime.update"):
        assert left[topic] == 1  # only the recent one
    for topic in ("trade.log", "stack.buy", "funds.deposit", "desk.control"):
        assert left[topic] == 2  # kept forever
    assert left["order.update"] == 2 and left["alert"] == 2  # 30 days: the 40-day-old alert went
    assert j.one("SELECT COUNT(*) FROM equity")[0] <= 2 and res["thinned"] >= 598  # one point per 10 minutes
    assert desk.journal.trades(None, None, 10, 0)["items"] is not None  # the trade history still reads fine
    assert hk.metrics["compactions"] == 1


def test_trade_history_and_chart_still_work_after_tidying(tmp_path):
    desk = make(str(tmp_path / "tz.db"))
    run(desk.start())
    for _ in range(300):
        run(desk.step())
    before = desk.journal.trades(None, None, 1000, 0)["total"]
    desk.bots["housekeeper"].tidy(at=time.time() + 10 * 86_400)  # pretend it's 10 days later
    assert desk.journal.trades(None, None, 1000, 0)["total"] == before
    assert json.dumps(desk.chart("paper", 0))
    run(desk.shutdown())
