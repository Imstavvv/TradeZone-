"""Linking a real-money broker from the app (a logged-in device needs no extra password)."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ORIGIN, run
from tradezone.brokers.alpaca import AlpacaBroker
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app


@pytest.fixture
def site(tmp_path):
    desk = TradingDesk(Settings(feed="coinbase", host="0.0.0.0"), journal=Journal(str(tmp_path / "tz.db")))
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as c:
        c.desk, c.sec = desk, desk.security
        c.post("/api/signin/code", json={"code": desk.security.recovery_code})
        yield c


def fresh(c):
    return TestClient(c.app, base_url=ORIGIN)


@pytest.fixture
def fake_alpaca(monkeypatch):
    accounts = {"AKGOODKEY123": {"cash": 1234.5, "equity": 1500.0}}

    async def account(self):
        return accounts.get(self.client.headers.get("APCA-API-KEY-ID"))
    monkeypatch.setattr(AlpacaBroker, "account", account)

    async def positions(self):
        return {}
    monkeypatch.setattr(AlpacaBroker, "positions", positions)  # (no real network in the tests)


def test_link_broker_from_the_app(site, fake_alpaca, tmp_path):
    h = {}
    assert site.get("/api/accounts").json()["broker"]["linked"] is False
    assert fresh(site).post("/api/accounts/broker", json={"key": "AKGOODKEY123", "secret": "s"}).status_code == 401
    r = site.post("/api/accounts/broker", json={"key": "PKPAPER", "secret": "s"}, headers=h)
    assert r.status_code == 400 and "practice (paper) keys" in r.json()["detail"]
    r = site.post("/api/accounts/broker", json={"key": "AKWRONG", "secret": "s"}, headers=h)
    assert r.status_code == 400 and "didn't accept" in r.json()["detail"]
    r = site.post("/api/accounts/broker", json={"key": " AKGOODKEY123 ", "secret": "s\n"}, headers=h).json()
    assert r["ok"] and r["account"]["cash"] == 1234.5 and r["ready"] and r["key"] == "…Y123"
    assert "AKGOODKEY123" not in site.get("/api/accounts").text  # never sent back
    assert site.get("/api/mode").json()["account"]["cash"] == 1234.5  # Use real money now works
    again = TradingDesk(Settings(feed="coinbase", host="0.0.0.0"), journal=site.desk.journal)
    again._unlink_alpaca_once()  # (the start-up steps that matter here, without the network)
    again.load_linked_broker()
    assert again.broker_info()["linked"] and again.live_broker is not None  # remembered across restarts
    run(again.live_broker.close())
    assert site.delete("/api/accounts/broker", headers=h).status_code == 200
    assert site.get("/api/accounts").json()["broker"]["linked"] is False


def test_logged_in_public_site_can_go_live_without_any_token(site, fake_alpaca):
    site.post("/api/accounts/broker", json={"key": "AKGOODKEY123", "secret": "s"})
    chk = site.get("/api/mode").json()
    assert chk["blockers"] == [] and chk["account"]["cash"] == 1234.5  # the owner is logged in
