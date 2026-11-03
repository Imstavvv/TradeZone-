"""Front-page PIN login on new devices and linking a real-money broker from the app."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ORIGIN, run
from tradezone.brokers.alpaca import AlpacaBroker
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app

PIN = "482915"


@pytest.fixture
def site(tmp_path):
    desk = TradingDesk(Settings(feed="coinbase", host="0.0.0.0"), journal=Journal(str(tmp_path / "tz.db")))
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as c:
        c.desk, c.sec = desk, desk.security
        c.post("/api/setup", json={"code": desk.security.recovery_code, "pin": PIN})
        yield c


def fresh(c):
    return TestClient(c.app, base_url=ORIGIN)


def test_new_device_logs_in_with_just_the_pin(site):
    phone = fresh(site)
    assert phone.get("/api/state").status_code == 401
    r = phone.post("/api/signin/pin", json={"pin": "000001"})
    assert r.status_code == 401 and "isn't right" in r.json()["detail"]
    assert phone.post("/api/signin/pin", json={"pin": PIN}).status_code == 200
    assert phone.get("/api/state").status_code == 200


def test_pin_login_is_capped_across_the_whole_internet(site):
    for i in range(site.sec.PIN_SIGNIN_MAX_PER_HOUR):
        fresh(site).post("/api/signin/pin", json={"pin": f"00000{i}"})
        site.sec.unban("testclient")  # pretend each guess came from a different computer
    r = fresh(site).post("/api/signin/pin", json={"pin": PIN})
    assert r.status_code == 429 and "paused" in r.json()["detail"]  # even the right PIN waits
    assert site.get("/api/state").status_code == 200  # devices already logged in are unaffected


@pytest.fixture
def fake_alpaca(monkeypatch):
    accounts = {"AKGOODKEY123": {"cash": 1234.5, "equity": 1500.0}}

    async def account(self):
        return accounts.get(self.client.headers.get("APCA-API-KEY-ID"))
    monkeypatch.setattr(AlpacaBroker, "account", account)


def test_link_broker_from_the_app(site, fake_alpaca, tmp_path):
    h = {"X-TZ-PIN": PIN}
    assert site.get("/api/accounts").json()["broker"]["linked"] is False
    assert site.post("/api/accounts/broker", json={"key": "AKGOODKEY123", "secret": "s"}).status_code == 403  # PIN needed
    r = site.post("/api/accounts/broker", json={"key": "PKPAPER", "secret": "s"}, headers=h)
    assert r.status_code == 400 and "practice (paper) keys" in r.json()["detail"]
    r = site.post("/api/accounts/broker", json={"key": "AKWRONG", "secret": "s"}, headers=h)
    assert r.status_code == 400 and "didn't accept" in r.json()["detail"]
    r = site.post("/api/accounts/broker", json={"key": " AKGOODKEY123 ", "secret": "s\n"}, headers=h).json()
    assert r["ok"] and r["account"]["cash"] == 1234.5 and r["ready"] and r["key"] == "…Y123"
    assert "AKGOODKEY123" not in site.get("/api/accounts").text  # never sent back
    assert site.get("/api/mode").json()["account"]["cash"] == 1234.5  # Use real money now works
    again = TradingDesk(Settings(feed="coinbase", host="0.0.0.0"), journal=site.desk.journal)
    run(again.start())
    assert again.broker_info()["linked"] and again.live_broker is not None  # remembered across restarts
    assert site.delete("/api/accounts/broker", headers=h).status_code == 200
    assert site.get("/api/accounts").json()["broker"]["linked"] is False


def test_public_site_with_a_pin_can_go_live_without_any_token(site, fake_alpaca):
    site.post("/api/accounts/broker", json={"key": "AKGOODKEY123", "secret": "s"}, headers={"X-TZ-PIN": PIN})
    chk = site.get("/api/mode").json()
    assert chk["blockers"] == [] and chk["account"]["cash"] == 1234.5  # the PIN protects real money now
