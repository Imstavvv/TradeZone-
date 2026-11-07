"""Every open trade has its own controls (asked for): sell all / part, and change its stop-loss and take-profit."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import prime, run
from tests.test_wide_stop import tick
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def opened():
    s = Settings(feed="sim", symbols=["BTC-USD", "ETH-USD"])
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    prime(d, "BTC-USD", 50_000.0, atr=250.0)
    d.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(d.bus.drain())
    assert d.portfolio.position("BTC-USD").qty > 0
    return d


def site(d):
    return TestClient(create_app(d, run_desk=False))


def test_sell_half_then_the_rest():
    d = opened()
    pos = d.portfolio.position("BTC-USD")
    had = pos.qty
    tick(d, "BTC-USD", pos.avg_price * 1.05)
    with site(d) as c:
        assert c.post("/api/trade/sell", json={"symbol": "btc-usd", "fraction": 0.5}).status_code == 200
        assert pos.qty == pytest.approx(had / 2, rel=0.02)
        assert c.post("/api/trade/sell", json={"symbol": "BTC-USD", "fraction": 1}).status_code == 200
    assert pos.is_flat


def test_a_sale_at_a_loss_needs_your_confirmation():
    d = opened()
    pos = d.portfolio.position("BTC-USD")
    tick(d, "BTC-USD", pos.avg_price * 0.97)  # 3% down
    with site(d) as c:
        r = c.post("/api/trade/sell", json={"symbol": "BTC-USD"})
        assert r.status_code == 409 and "loses about" in r.json()["detail"] and pos.qty > 0  # not sold yet
        assert c.post("/api/trade/sell", json={"symbol": "BTC-USD", "confirm_loss": True}).status_code == 200
    assert pos.is_flat


def test_change_the_stop_and_target_within_the_rules():
    d = opened()
    pos = d.portfolio.position("BTC-USD")
    with site(d) as c:
        r = c.post("/api/trade/plan", json={"symbol": "BTC-USD", "stop_pct": 0.04, "target_pct": 0.25})
        assert r.status_code == 200
        assert pos.stop_price == pytest.approx(pos.avg_price * 0.96)
        assert pos.take_profit == pytest.approx(pos.avg_price * 1.25 + d.settings.round_trip_cost("BTC-USD", pos.avg_price))
        assert c.post("/api/trade/plan", json={"symbol": "BTC-USD", "stop_pct": 0.20}).status_code == 400  # past -7%
        assert c.post("/api/trade/plan", json={"symbol": "ETH-USD", "stop_pct": 0.05}).status_code == 400  # no trade
        assert c.post("/api/trade/sell", json={"symbol": "BTC-USD", "account": "nope"}).status_code == 404


def test_the_plan_is_shown_as_it_was_saved_so_saving_again_changes_nothing():
    d = opened()
    pos = d.portfolio.position("BTC-USD")
    d.set_trade_plan("BTC-USD", 0.05, 0.20)
    row = next(x for x in d.portfolio_view()["positions"] if x["symbol"] == "BTC-USD")
    assert row["stop_pct"] == pytest.approx(0.05) and row["target_pct"] == pytest.approx(0.20)  # (not 20% + the fees)
    tp = pos.take_profit
    d.set_trade_plan("BTC-USD", row["stop_pct"], row["target_pct"])
    assert pos.take_profit == pytest.approx(tp)  # saving what's shown doesn't creep the target up
