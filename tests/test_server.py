from fastapi.testclient import TestClient

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app


def make(token=""):
    s = Settings(api_token=token, feed="sim")
    desk = TradingDesk(s, journal=Journal(":memory:"))
    return desk, TestClient(create_app(desk, run_desk=False))


def test_state_controls_and_bots():
    desk, c = make()
    with c:
        assert c.get("/api/health").json()["ok"]
        st = c.get("/api/state").json()
        assert len(st["bots"]) == 40 and st["mode"] == "paper"
        assert c.post("/api/control/pause").json()["paused"] is True
        assert c.post("/api/control/resume").json()["paused"] is False
        assert c.post("/api/control/kill", json={"reason": "test"}).json()["halted"] is True
        assert c.post("/api/control/reset").json()["halted"] is False
        assert c.post("/api/control/bogus").status_code == 400
        assert c.post("/api/bots/quant", json={"enabled": False}).json()["enabled"] is False
        assert desk.bots["quant"].enabled is False
        assert c.post("/api/bots/nope", json={"enabled": False}).status_code == 404
        assert "TradeZone" in c.get("/").text
        assert c.get("/manifest.webmanifest").json()["short_name"] == "TradeZone"
        assert c.get("/icon-180.png").headers["content-type"] == "image/png"
        assert c.get("/icon.svg").status_code == 200
        f = c.get("/fonts/inter.woff2")
        assert f.status_code == 200 and f.headers["content-type"] == "font/woff2" and f.content[:4] == b"wOF2"
        assert c.get("/fonts/missing.woff2").status_code == 404  # only the two bundled fonts
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_token_required_when_configured():
    _, c = make("s3cret")
    with c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/control/kill").status_code == 401
        assert c.get("/api/state", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/health").status_code == 200
        assert c.get("/fonts/inter.woff2").status_code == 200  # the login screen needs its font before logging in
