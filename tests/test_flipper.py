"""Flipper: learns the best hours to flip, sizes trades by them, and flips moves on heavy volume.
Reinvest: a share of today's banked profit goes into the next trades."""

import time

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Snapshot
from tradezone.core.portfolio import ClosedTrade
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(journal=None, **kw):
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD", "SOL-USD"], **kw)
    s.risk.symbol_cooldown_s = 0
    s.risk.max_position_pct = s.risk.max_gross_exposure = 5.0  # size from the risk, not the caps
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=journal or Journal(":memory:"))


def learn(desk, hour, results):
    f = desk.bots["flipper"]
    for i, pnl in enumerate(results):
        sym = f"X{i}-USD"
        f.on_trade(type("E", (), {"payload": {"symbol": sym, "action": "open", "ts": 1_700_000_000 + hour * 3600}})())
        f.on_trade(type("E", (), {"payload": {"symbol": sym, "action": "close", "pnl": pnl, "position_after": 0.0,
                                              "ts": 1_700_000_000 + hour * 3600 + 60}})())


def hour_now():
    return time.gmtime().tm_hour


def buy(desk, sym="BTC-USD"):
    prime(desk, sym, 50_000.0, atr=250.0)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym).qty


def test_it_learns_which_hours_make_money():
    desk = make()
    f = desk.bots["flipper"]
    base = 1_700_000_000 // 3600 % 24  # the hour of the fake timestamps' day start
    learn(desk, 0, [5, 4, -1, 6, 3, 2, -2, 4, 3, 2, -1, 5])  # 12 trades, mostly winners
    learn(desk, 1, [-3, -4, 1, -2, -5, -1, -2, -3, 2, -1, -2, -4])  # 12 trades, mostly losers
    good, bad = (base) % 24, (base + 1) % 24
    assert f.rating(good) == "good" and f.rating(bad) == "bad" and f.rating((base + 5) % 24) == "learning"
    assert f.best_hours()[0]["hour"] == good and f.best_hours(worst=True)[0]["hour"] == bad


def test_good_hours_trade_bigger_and_bad_hours_smaller():
    normal = buy(make())
    good = make()
    good.bots["flipper"].hours[str(hour_now())] = {"n": 12, "wins": 9, "pnl": 50.0}
    bad = make()
    bad.bots["flipper"].hours[str(hour_now())] = {"n": 12, "wins": 3, "pnl": -50.0}
    assert buy(good) == pytest.approx(normal * 1.25, rel=0.02)
    assert buy(bad) == pytest.approx(normal * 0.6, rel=0.02)


def flip_snapshot(desk, sym="SOL-USD", vr=2.5):
    price = 150.0
    prime(desk, sym, price, atr=0.6)
    ind = {"atr": 0.6, "volatility": 0.002, "rsi": 60, "ema_fast": price * 0.999, "ema_slow": price * 0.997,
           "sma50": price * 0.99, "donchian_high": price * 1.01, "donchian_low": price * 0.95,
           "swing_low": price * 0.98, "swing_high": price * 1.01, "volume_ratio": vr}
    snap = Snapshot(sym, price, ind)
    desk.bots["risk"].snapshots[sym] = snap
    desk.bus.publish("market.snapshot", snap)
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def test_it_flips_a_move_on_heavy_volume():
    desk = make()
    desk.set_bot_enabled("quant", False)
    desk.set_bot_enabled("skimmer", False)
    ideas = capture(desk, "signal.proposed")
    assert flip_snapshot(desk).qty > 0
    assert any(i.source == "flipper" for i in ideas) and desk.bots["flipper"].metrics["flips"] == 1


def test_no_flip_without_volume_or_in_a_losing_hour():
    desk = make()
    desk.set_bot_enabled("quant", False)
    desk.set_bot_enabled("skimmer", False)
    assert flip_snapshot(desk, vr=1.1).is_flat  # a move on ordinary volume isn't a flip
    bad = make()
    bad.set_bot_enabled("quant", False)
    bad.set_bot_enabled("skimmer", False)
    bad.bots["flipper"].hours[str(hour_now())] = {"n": 12, "wins": 3, "pnl": -50.0}
    assert flip_snapshot(bad).is_flat


def test_what_it_learned_survives_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    d1 = make(Journal(db))
    d1.bots["flipper"].hours["14"] = {"n": 9, "wins": 6, "pnl": 12.5}
    run(d1.shutdown())
    d2 = make(Journal(db))
    run(d2.start())
    assert d2.bots["flipper"].hours["14"]["pnl"] == 12.5
    assert "flipper" in d2.state()
    run(d2.shutdown())


def test_banked_profit_is_reinvested_into_the_next_trades():
    plain = buy(make(reinvest_share=0))
    desk = make()
    desk.portfolio.closed_trades.append(ClosedTrade("ETH-USD", "long", 1, 3000, 3200, 400.0))  # +$400 banked today
    more = buy(desk)
    per_coin = 50_000.0 * desk.settings.major_stop_loss_pct + desk.settings.round_trip_cost("BTC-USD", 50_000.0)
    extra = (more - plain) * per_coin  # the extra money at risk (stop distance + fees, as the sizing counts it)
    assert more > plain and extra == pytest.approx(0.25 * 400.0, rel=0.1)  # a quarter of it works again
