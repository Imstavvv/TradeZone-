"""Simple sign-in: no password, no Face ID - a code from the server logs or from a logged-in device."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ORIGIN
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app


@pytest.fixture
def site():
    """A public site with no access token configured (the default on Render)."""
    desk = TradingDesk(Settings(feed="sim", host="0.0.0.0", withdrawal_cooldown_h=0), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as c:
        c.desk, c.sec = desk, desk.security
        yield c


def new_device(c):
    return TestClient(c.app, base_url=ORIGIN)


def log_in(c, code=None):
    return c.post("/api/signin/code", json={"code": code or c.sec.recovery_code})


def test_first_login_with_the_logged_code(site):
    assert site.get("/api/setup/status").json() == {"owner": False, "auth_required": True}
    assert site.get("/api/state").status_code == 401  # closed until someone logs in
    assert log_in(site, "0000-0000").status_code == 401
    code = site.sec.recovery_code
    r = log_in(site, code)
    assert r.status_code == 200 and site.get("/api/state").status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=31536000" in cookie  # a year
    assert site.get("/api/setup/status").json()["owner"] is True
    assert site.sec.recovery_code != code  # single use: a fresh code is in the logs
    assert log_in(new_device(site), code).status_code == 401


def test_another_device_logs_in_with_a_6_digit_code(site):
    log_in(site)
    code = site.post("/api/devices/pair").json()["code"]
    phone = new_device(site)
    assert phone.get("/api/state").status_code == 401
    assert phone.post("/api/signin/code", json={"code": code}).status_code == 200
    assert phone.get("/api/state").status_code == 200
    assert new_device(site).post("/api/signin/code", json={"code": code}).status_code == 401  # works once
    assert new_device(site).post("/api/devices/pair").status_code == 401  # only a logged-in device makes codes


def test_pairing_codes_expire_and_burn_out(site):
    log_in(site)
    code = site.post("/api/devices/pair").json()["code"]
    for _ in range(5):
        new_device(site).post("/api/signin/code", json={"code": "000000" if code != "000000" else "111111"})
    site.sec.unban("testclient")  # (the guesses also banned this address)
    assert new_device(site).post("/api/signin/code", json={"code": code}).status_code == 401  # burned by guessing


def test_wrong_codes_ban_the_guesser_and_rotate_the_log_code(site):
    code = site.sec.recovery_code
    for i in range(5):
        new_device(site).post("/api/signin/code", json={"code": f"1111-111{i}"})
    assert new_device(site).get("/api/setup/status").status_code == 429  # the guessing address is banned
    site.sec.unban("testclient")
    for i in range(10):
        site.sec.check_recovery(f"2222-222{i}")
    assert site.sec.recovery_code != code  # too many wrong guesses: a new code is written to the logs


def test_logged_in_device_does_everything_without_a_password(site):
    log_in(site)
    body = {"chain": "XRP", "address": "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe", "tag": "7"}
    assert site.post("/api/wallets", json=body).status_code == 200
    assert site.post("/api/logout").status_code == 200
    assert site.get("/api/state").status_code == 401


def test_old_password_and_face_id_routes_are_gone(site):
    log_in(site)
    for path in ("/api/setup", "/api/signin/pin", "/api/signin/faceid/options", "/api/unlock/pin",
                 "/api/applock/pin", "/api/webauthn/register/options", "/api/lock"):
        assert site.post(path, json={}).status_code in (404, 405), path


def test_site_set_up_with_a_password_before_stays_owned(tmp_path):
    db = str(tmp_path / "tz.db")
    j = Journal(db)
    j.run("CREATE TABLE app_lock (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    j.run("INSERT INTO app_lock VALUES ('pin', '{}')")
    desk = TradingDesk(Settings(feed="sim", host="0.0.0.0"), journal=j)
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as c:
        assert c.get("/api/setup/status").json()["owner"] is True  # nobody else can "claim" it


def test_local_dev_stays_open_until_someone_logs_in():
    desk = TradingDesk(Settings(feed="sim"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as c:
        assert c.get("/api/state").status_code == 200
