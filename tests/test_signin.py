"""Token-free access: first-time setup, Face ID sign-in, device pairing, recovery, PIN/Face ID step-up."""

import time

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ORIGIN, SoftAuthenticator
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app

PIN = "482915"
XRP = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


@pytest.fixture
def site():
    """A public site with NO access token configured (the new default)."""
    desk = TradingDesk(Settings(feed="sim", host="0.0.0.0", withdrawal_cooldown_h=0), journal=Journal(":memory:"))
    app = create_app(desk, run_desk=False)
    with TestClient(app, base_url=ORIGIN) as c:
        c.desk, c.sec = desk, desk.security
        yield c


def new_device(c):
    return TestClient(c.app, base_url=ORIGIN)


def setup(c, code=None):
    r = c.post("/api/setup", json={"code": code or c.sec.recovery_code, "pin": PIN})
    c.app.state.expire_confirmations()  # logging in counts as a confirmation for a few minutes; these tests check the step-up itself
    return r


def test_first_setup_needs_the_logged_code_once(site):
    assert site.get("/api/setup/status").json()["owner"] is False
    assert site.get("/api/state").status_code == 401  # closed until set up
    assert setup(site, "0000-0000").status_code == 401
    code = site.sec.recovery_code
    assert setup(site, code).status_code == 200
    assert site.get("/api/state").status_code == 200  # signed in and unlocked
    assert site.get("/api/setup/status").json()["owner"] is True
    assert setup(new_device(site), code).status_code == 409  # nobody can set it up twice
    assert site.sec.recovery_code != code  # single use


def test_weak_pin_refused_at_setup(site):
    r = site.post("/api/setup", json={"code": site.sec.recovery_code, "pin": "123456"})
    assert r.status_code == 400 and "stronger" in r.json()["detail"]


def test_add_a_device_with_a_pairing_code(site):
    setup(site)
    assert site.post("/api/devices/pair").status_code == 403  # creating access needs the PIN
    code = site.post("/api/devices/pair", headers={"X-TZ-PIN": PIN}).json()["code"]
    phone = new_device(site)
    assert phone.get("/api/state").status_code == 401
    assert phone.post("/api/signin/pair", json={"code": code}).status_code == 200
    assert phone.get("/api/state").status_code == 200
    assert new_device(site).post("/api/signin/pair", json={"code": code}).status_code == 401  # single use


def test_pairing_codes_expire_and_burn_out(site):
    setup(site)
    code = site.post("/api/devices/pair", headers={"X-TZ-PIN": PIN}).json()["code"]
    attacker = new_device(site)
    for g in ("000000", "111111", "222222", "333333", "444444"):
        if g != code:
            attacker.post("/api/signin/pair", json={"code": g})
    assert attacker.post("/api/signin/pair", json={"code": code}).status_code == 429  # attacker's IP is banned
    site.sec.unban("testclient")  # (test clients share one IP; the owner would be on another)
    assert new_device(site).post("/api/signin/pair", json={"code": code}).status_code == 401  # burned
    code2 = site.post("/api/devices/pair", headers={"X-TZ-PIN": PIN}).json()["code"]
    site.sec._pairing[code2][0] = time.time() - 1  # expired
    assert new_device(site).post("/api/signin/pair", json={"code": code2}).status_code == 401


def test_recovery_code_restores_access_and_rotates(site):
    setup(site)
    lost = new_device(site)
    old = site.sec.recovery_code
    assert lost.post("/api/signin/recover", json={"code": "1234-5678"}).status_code == 401
    assert lost.post("/api/signin/recover", json={"code": old}).status_code == 200
    assert lost.get("/api/state").status_code == 200
    assert site.sec.recovery_code != old
    # right after recovery the owner can choose a new PIN without the forgotten one
    assert lost.post("/api/applock/pin", json={"pin": "907153"}).status_code == 200
    for _ in range(site.sec.RECOVERY_MAX_ATTEMPTS):  # brute force rotates the code
        site.sec.check_recovery("0000-0001")
    assert site.sec._recovery_attempts == 0


def test_face_id_sign_in_on_a_new_device(site):
    setup(site)
    auth = SoftAuthenticator()
    opts = site.post("/api/webauthn/register/options", headers={"X-TZ-PIN": PIN}).json()
    assert site.post("/api/webauthn/register/verify", json={"credential": auth.create(opts)}).status_code == 200
    assert site.get("/api/setup/status").json()["faceid"] is True
    laptop = new_device(site)
    r = laptop.post("/api/signin/faceid/options").json()
    assert laptop.post("/api/signin/faceid/verify",
                       json={"ceremony": r["ceremony"], "credential": auth.get(r["options"])}).status_code == 200
    assert laptop.get("/api/state").status_code == 200
    stranger = SoftAuthenticator()
    stranger.cred_id = b"\x09" * 16
    r = new_device(site).post("/api/signin/faceid/options").json()
    assert new_device(site).post("/api/signin/faceid/verify",
                                 json={"ceremony": r["ceremony"], "credential": stranger.get(r["options"])}).status_code == 401


def test_pin_and_face_id_confirm_money_moves(site):
    setup(site)
    body = {"chain": "XRP", "address": XRP, "tag": "7"}
    assert site.post("/api/wallets", json=body).status_code == 403
    r = site.post("/api/wallets", json=body, headers={"X-TZ-PIN": "000001"})
    assert r.status_code == 403 and "tries left" in r.json()["detail"]
    assert site.post("/api/wallets", json=body, headers={"X-TZ-PIN": PIN}).status_code == 200
    auth = SoftAuthenticator()
    opts = site.post("/api/webauthn/register/options", headers={"X-TZ-PIN": PIN}).json()
    site.post("/api/webauthn/register/verify", json={"credential": auth.create(opts)})
    opts = site.post("/api/confirm/faceid/options").json()
    assert site.post("/api/confirm/faceid/verify", json={"credential": auth.get(opts)}).status_code == 200
    body2 = {"chain": "XRP", "address": XRP, "tag": "8"}
    assert site.post("/api/wallets", json=body2).status_code == 200  # Face ID confirmation...
    assert site.post("/api/wallets", json={**body2, "tag": "9"}).status_code == 200  # ...counts for a few minutes
    site.app.state.expire_confirmations()
    assert site.post("/api/wallets", json={**body2, "tag": "10"}).status_code == 403  # then asks again


def test_local_dev_stays_open_until_a_pin_exists():
    desk = TradingDesk(Settings(feed="sim"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as c:
        assert c.get("/api/state").status_code == 200


def test_cannot_remove_the_last_way_in(site):
    setup(site)
    r = site.delete("/api/applock/pin", headers={"X-TZ-PIN": PIN})
    assert r.status_code == 409 and "only way in" in r.json()["detail"]


def test_after_setup_the_log_shows_a_recovery_code(site, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="tradezone.security"):
        setup(site)
    assert "TradeZone recovery code: " + site.sec.recovery_code in caplog.text
