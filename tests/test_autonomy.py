"""Restart recovery, daily re-arm and realistic-cost behaviour."""

import pytest

from tests.helpers import FakeLiveBroker, capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def live_settings(**kw):
    kw.setdefault("symbols", ["BTC-USD", "AAPL", "NVDA"])
    s = Settings(never_sell_at_loss=False, feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", **kw)
    s.risk.symbol_cooldown_s = 0
    return s


def make(path, live=None, **kw):
    s = live_settings(**kw)
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(str(path)),
                       live_broker=live or FakeLiveBroker())


def open_aapl(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("AAPL")
    assert pos.qty > 0
    return pos


def test_paper_positions_and_stops_survive_restart(tmp_path):
    db = tmp_path / "tz.db"
    d1 = make(db)
    pos = open_aapl(d1)
    d1.bots["portfolio"].protected.add("AAPL")
    d1.save_state()
    d2 = make(db)
    run(d2.start())
    p2 = d2.portfolio.position("AAPL")
    assert p2.qty == pos.qty and p2.stop_price == pos.stop_price and p2.take_profit == pos.take_profit
    assert d2.portfolio.cash == pytest.approx(d1.portfolio.cash)
    assert "AAPL" in d2.bots["portfolio"].protected and "AAPL" in d2.bots["portfolio"].trail_dist
    assert d2.bots["sentinel"].audit_books() == []  # books balanced after restore
    # the restored stop is live: a crash through it closes the position
    d2.prices["AAPL"] = pos.stop_price * 0.99
    d2.paper_broker.on_prices([Tick("AAPL", pos.stop_price * 0.99)])
    d2.bus.publish("market.tick", Tick("AAPL", pos.stop_price * 0.99))
    run(d2.bus.drain())
    assert d2.portfolio.position("AAPL").is_flat


def test_live_mode_resumes_when_broker_matches(tmp_path):
    db = tmp_path / "tz.db"
    live = FakeLiveBroker(cash=5_000)
    d1 = make(db, live)
    prime(d1)
    run(d1.switch_mode("live"))
    open_aapl(d1)
    qty = d1.portfolio.position("AAPL").qty
    d1.save_state()
    live2 = FakeLiveBroker(cash=5_000, positions={"AAPL": qty})  # broker agrees with our book
    d2 = make(db, live2)
    run(d2.start())
    assert d2.is_live and d2.broker is live2
    assert d2.portfolio.position("AAPL").qty == qty


def test_live_not_resumed_when_broker_disagrees(tmp_path):
    db = tmp_path / "tz.db"
    d1 = make(db, FakeLiveBroker(cash=5_000))
    prime(d1)
    run(d1.switch_mode("live"))
    open_aapl(d1)
    d1.save_state()
    d2 = make(db, FakeLiveBroker(cash=5_000, positions={}))  # position closed outside TradeZone
    alerts = capture(d2, "alert")
    run(d2.start())
    assert d2.is_live  # resumes; the records' check sorts out the difference instead of refusing
    pm = d2.bots["portfolio"]
    pm.last_fill_ts = 0
    run(pm.reconcile_positions())
    fixed = run(pm.reconcile_positions())
    run(d2.bus.drain())
    assert fixed and d2.portfolio.position("AAPL").is_flat and not d2.paused
    assert any("Records updated to match your account" in a.message for a in alerts)
    assert d2.bots["sentinel"].audit_books() == []


def test_resume_can_be_disabled(tmp_path):
    db = tmp_path / "tz.db"
    d1 = make(db)
    open_aapl(d1)
    d1.save_state()
    d2 = make(db, resume_on_restart=False)
    run(d2.start())
    assert d2.portfolio.position("AAPL").is_flat


def test_daily_kill_switch_rearms_next_day(desk):
    desk.halt("daily loss", flatten=False, kind="daily")
    desk._day = "2000-01-01"  # pretend the day just rolled over
    run(desk.step())
    assert not desk.halted


def test_drawdown_kill_switch_stays_down(desk):
    desk.halt("drawdown", flatten=False, kind="drawdown")
    desk._day = "2000-01-01"
    run(desk.step())
    assert desk.halted


def test_realistic_crypto_fees_and_fee_filter(desk):
    s = desk.settings
    assert s.fee_bps("BTC-USD") == 25 and s.fee_bps("AAPL") == 0
    # paper fills charge the crypto fee
    prime(desk, "BTC-USD", 60_000.0, atr=600.0)
    fills = capture(desk, "order.filled")
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    f = fills[0]
    assert f.commission == pytest.approx(f.qty * f.price * 0.0025)
    # a target too small to pay for fees is skipped
    s.stop_method = "atr"  # volatility-based target (a fixed +15% target always clears fees)
    prime(desk, "NVDA", 100.0, atr=0.01)  # tiny volatility -> tiny target
    s.stock_fee_bps = 25
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("NVDA", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert "doesn't clear fees" in rejected[-1]["reason"]


def test_todays_hunt_counts_survive_a_restart(tmp_path):
    db = tmp_path / "tz.db"
    d1 = make(db)
    open_aapl(d1)
    d1.hunt["checks"] = 120
    d1.save_state()
    d2 = make(db)
    run(d2.start())
    assert d2.hunt_summary()["checks"] == 120 and d2.hunt_summary()["opened"] == d1.hunt["opened"]
    run(d2.shutdown())
