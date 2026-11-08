"""One-tap live (asked for: "live switch toggle - I understand the risk"): the choice is saved once, on the server."""

from fastapi.testclient import TestClient

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def test_the_risk_is_acknowledged_once_and_can_be_taken_back():
    d = TradingDesk(Settings(feed="sim", symbols=["BTC-USD"]), feed=SimulatedFeed(["BTC-USD"]), journal=Journal(":memory:"))
    with TestClient(create_app(d, run_desk=False)) as c:
        assert c.get("/api/mode").json()["live_ack"] is False  # (asks every time until you say so)
        assert c.post("/api/live-ack", json={"on": True}).json()["live_ack"] is True
        assert c.get("/api/mode").json()["live_ack"] is True
        assert c.post("/api/live-ack", json={"on": False}).json()["live_ack"] is False
        # the server still won't switch without the word: a script can't go live by accident
        assert c.post("/api/mode", json={"mode": "live"}).status_code == 400
