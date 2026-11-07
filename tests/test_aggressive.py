"""Aggressive risk (real money only): bigger bets and wider loss limits - stops and no-leverage stay."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import FakeLiveBroker, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def make(profile="normal", journal=None, cash=10_000.0):
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", symbols=["BTC-USD", "ETH-USD"],
                 risk_profile=profile)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=journal or Journal(":memory:"),
                       live_broker=FakeLiveBroker(cash=cash))


def buy(desk, strength=0.9):
    prime(desk, "BTC-USD", 50_000.0, atr=250.0)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, strength, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position("BTC-USD")


def at_risk(pos):
    """Money lost at the patient cut (-5%) the trade is sized for (its -33% stop is the last-resort backstop)."""
    return pos.qty * pos.avg_price * 0.05


def test_normal_now_trades_as_big_as_aggressive_with_real_money():
    """All the capital at work (asked for): Normal real-money trades are Aggressive-sized; Aggressive keeps its wider
    loss limits."""
    normal, agg = make(), make("aggressive")
    for d in (normal, agg):
        prime(d, "BTC-USD", 50_000.0, atr=250.0)
        run(d.switch_mode("live"))
    n, a = at_risk(buy(normal)), at_risk(buy(agg))
    assert a == pytest.approx(n)  # the same size now (4% risk, up to 50% of the balance)
    assert a <= 10_000 * 0.04 * 1.01  # ...never more than 4% of the account to the stop
    assert buy(agg).stop_price is not None  # every trade still has a stop


def test_practice_money_is_never_affected():
    assert at_risk(buy(make("aggressive"))) == pytest.approx(at_risk(buy(make())), rel=0.01)


def test_aggressive_has_wider_real_money_loss_limits():
    for profile, halted in (("normal", True), ("aggressive", False)):
        d = make(profile)
        prime(d, "BTC-USD", 50_000.0, atr=250.0)
        run(d.switch_mode("live"))
        d.portfolio.cash -= 600.0  # -6% today
        run(d.bots["risk"].on_cycle())
        assert d.halted is halted
    d = make("aggressive")
    prime(d, "BTC-USD", 50_000.0, atr=250.0)
    run(d.switch_mode("live"))
    d.portfolio.cash -= 1_100.0  # -11%: past even Aggressive's 10% daily limit
    run(d.bots["risk"].on_cycle())
    assert d.halted


def test_turning_aggressive_on_needs_the_word_risk_and_is_remembered(tmp_path):
    db = str(tmp_path / "tz.db")
    d = make(journal=Journal(db))
    with pytest.raises(ValueError, match="type RISK"):
        d.set_risk_profile("aggressive")
    assert d.set_risk_profile("aggressive", "risk")["profile"] == "aggressive"
    d.save_state()
    d2 = make(journal=Journal(db))
    run(d2.restore_state())
    assert d2.settings.risk_profile == "aggressive"
    assert d2.set_risk_profile("normal")["risk_per_trade"] == pytest.approx(0.04)  # back to Normal: no word needed


def test_risk_level_api():
    desk = make()
    c = TestClient(create_app(desk))
    assert c.post("/api/risk-profile", json={"profile": "aggressive"}).status_code == 400
    r = c.post("/api/risk-profile", json={"profile": "aggressive", "confirm": "RISK"})
    assert r.status_code == 200 and r.json()["risk_per_trade"] == 0.04
    assert desk.state()["live_risk"]["active"] and not desk.state()["limits"]["active"]  # practice unaffected


def test_bold_bets_bigger_again_and_needs_the_word_risk():
    normal, agg, bold = make(), make("aggressive"), make()
    with pytest.raises(ValueError, match="RISK"):
        bold.set_risk_profile("bold")
    info = bold.set_risk_profile("bold", "risk")
    assert info["risk_per_trade"] == 0.06 and info["max_position_pct"] == 0.70 and info["max_daily_loss_pct"] == 0.15
    assert bold.settings.live_limits() == (0.15, 0.40, 0.50)
    for d in (normal, agg, bold):
        prime(d, "BTC-USD", 50_000.0, atr=250.0)
        run(d.switch_mode("live"))
    n, a, b = (at_risk(buy(d)) for d in (normal, agg, bold))
    assert n <= a < b <= 10_000 * 0.06 * 1.01  # Bold bigger again; never more than 6% to the early cut
    assert buy(bold).stop_price is not None  # every trade still has a stop
    assert bold.portfolio.cash >= 0  # never borrows


def test_bold_never_touches_practice_money():
    d = make()
    d.set_risk_profile("bold", "RISK")
    assert at_risk(buy(d)) == pytest.approx(at_risk(buy(make())), rel=0.01)


def test_bold_is_remembered_after_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    d1 = make(journal=Journal(db))
    d1.set_risk_profile("bold", "RISK")
    d1.save_state()
    d2 = make(journal=Journal(db))
    run(d2.restore_state())
    assert d2.settings.risk_profile == "bold"
