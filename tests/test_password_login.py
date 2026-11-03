"""Easier login: a password the phone can save (old 6-digit PINs still work), staying logged in."""

from fastapi.testclient import TestClient

from tests.helpers import ORIGIN
from tests.test_signin import PIN, new_device, site  # noqa: F401  (fixture)
from tradezone.applock import weak_pin_reason
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app

PW = "sunny meadow 42 ☀️"


def test_password_rules():
    assert weak_pin_reason("sunny-meadow") is None
    assert weak_pin_reason("482915") is None  # old 6-digit PINs still fine
    assert "at least 6" in weak_pin_reason("abc")
    assert "common" in weak_pin_reason("Password")
    assert weak_pin_reason("123456") and weak_pin_reason("aaaaaaaa")


def test_set_up_and_log_in_with_a_password(site):  # noqa: F811
    assert site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": PW}).status_code == 200
    phone = new_device(site)
    assert phone.post("/api/signin/pin", json={"pin": "nope-nope"}).status_code == 401
    assert phone.post("/api/signin/pin", json={"pin": PW}).status_code == 200
    assert phone.get("/api/state").status_code == 200
    cookie = phone.cookies.jar
    assert any(c.name == "tz_session" for c in cookie)


def test_an_existing_pin_keeps_working(site):  # noqa: F811
    site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": PIN})
    assert new_device(site).post("/api/signin/pin", json={"pin": PIN}).status_code == 200


def test_typos_dont_ban_the_owners_phone(site):  # noqa: F811
    site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": PW})
    phone = new_device(site)
    for i in range(6):
        assert phone.post("/api/signin/pin", json={"pin": f"typo-{i}-xx"}).status_code == 401
    assert phone.post("/api/signin/pin", json={"pin": PW}).status_code == 200  # not banned


def test_stays_logged_in_no_auto_lock(site):  # noqa: F811
    site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": PW})
    a = site.get("/api/auth").json()
    assert a["locked"] is False and a["lock"]["lock_after_min"] == 0
    assert site.post("/api/lock").status_code == 200  # "Lock now" still works on demand
    assert site.get("/api/state").status_code == 423
    assert site.post("/api/unlock/pin", json={"pin": PW}).status_code == 200


def test_password_with_any_characters_confirms_actions(site):  # noqa: F811
    from urllib.parse import quote
    site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": PW})
    site.app.state.expire_confirmations()
    body = {"chain": "XRP", "address": "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe", "tag": "1"}
    assert site.post("/api/wallets", json=body, headers={"X-TZ-PIN": quote(PW)}).status_code == 200


def test_cookie_survives_home_screen_launch(site):  # noqa: F811
    r = site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": PW})
    assert "samesite=lax" in r.headers["set-cookie"].lower()


def test_journal_goes_on_the_persistent_disk_when_there_is_one(tmp_path, monkeypatch):
    s = Settings(feed="sim")
    s.validate()
    assert s.journal_path in ("tradezone.db", "/data/tradezone.db")
    s2 = Settings(feed="sim", journal_path=str(tmp_path / "x.db"))
    s2.validate()
    assert s2.journal_path == str(tmp_path / "x.db")  # an explicit path always wins
    desk = TradingDesk(Settings(feed="sim"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as c:
        assert c.get("/api/setup/status").status_code == 200
