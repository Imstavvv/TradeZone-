"""App lock: 6-digit PIN and Face ID (WebAuthn passkeys), verified with a real software authenticator."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ORIGIN, SoftAuthenticator
from tradezone.applock import MAX_PIN_FAILURES, weak_pin_reason
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app

TOKEN = "correct-horse-battery-staple-42"


CONFIRM = {"X-TZ-Confirm": TOKEN}


@pytest.fixture
def c():
    desk = TradingDesk(Settings(feed="sim", api_token=TOKEN), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as client:
        client.desk = desk
        assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
        client.app.state.expire_confirmations()  # logging in counts as a confirmation for a few minutes; these tests check the step-up itself
        yield client


def lock(c):
    assert c.post("/api/lock").status_code == 200


@pytest.mark.parametrize("pin,ok", [("482915", True), ("123456", False), ("111111", False), ("987654", False),
                                    ("121212", False), ("4829", False), ("abcdef", False), ("112112", False)])
def test_pin_strength(pin, ok):
    assert (weak_pin_reason(pin) is None) == ok


def test_no_lock_until_user_chooses_one(c):
    assert c.get("/api/auth").json()["locked"] is False
    lock(c)
    assert c.get("/api/state").status_code == 200  # nothing configured: lock is off


def test_setting_pin_needs_token_and_rejects_weak(c):
    assert c.post("/api/applock/pin", json={"pin": "482915"}).status_code == 403  # step-up required
    r = c.post("/api/applock/pin", json={"pin": "123456"}, headers=CONFIRM)
    assert r.status_code == 400 and "stronger" in r.json()["detail"]
    assert c.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM).status_code == 200
    assert c.get("/api/state").status_code == 200  # still unlocked right after setting it


def test_locked_app_hides_everything_until_pin(c):
    c.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM)
    lock(c)
    a = c.get("/api/auth").json()
    assert a["locked"] is True and a["lock"]["pin"] is True
    for path in ("/api/state", "/api/trades", "/api/security", "/api/bots"):
        assert c.get(path).status_code == 423
    assert c.post("/api/control/kill").status_code == 423
    r = c.post("/api/unlock/pin", json={"pin": "000001"})
    assert r.status_code == 403 and f"{MAX_PIN_FAILURES - 1} tries left" in r.json()["detail"]
    assert c.post("/api/unlock/pin", json={"pin": "482915"}).status_code == 200
    assert c.get("/api/state").status_code == 200


def test_five_wrong_pins_sign_the_device_out(c):
    c.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM)
    lock(c)
    for i in range(MAX_PIN_FAILURES - 1):
        assert c.post("/api/unlock/pin", json={"pin": f"00000{i}"}).status_code == 403
    r = c.post("/api/unlock/pin", json={"pin": "000009"})
    assert r.status_code == 401 and "logged out" in r.json()["detail"]
    assert c.post("/api/unlock/pin", json={"pin": "482915"}).status_code == 401  # even the right PIN: token needed
    assert c.post("/api/login", json={"token": TOKEN}).status_code == 200  # access token restores the device
    assert c.get("/api/state").status_code == 200  # ...and counts as unlocked ("Forgot PIN?")
    kinds = [e["kind"] for e in c.desk.security.events]
    assert kinds.count("pin_failed") == MAX_PIN_FAILURES - 1 and "pin_lockout" in kinds


def test_lock_engages_after_inactivity(c):
    c.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM)
    with c.desk.journal._lock:
        c.desk.journal.conn.execute("UPDATE sessions SET unlocked_until = 1")  # pretend 5 idle minutes passed
    assert c.get("/api/state").status_code == 423


def test_websocket_refused_while_locked():
    from starlette.websockets import WebSocketDisconnect
    desk = TradingDesk(Settings(feed="sim", api_token=TOKEN), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as plain:  # http: the test client sends cookies on ws
        plain.post("/api/login", json={"token": TOKEN})
        plain.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM)
        with plain.websocket_connect("/ws") as ws:  # unlocked: streams
            assert ws.receive_json()["type"] == "snapshot"
        plain.post("/api/lock")
        with pytest.raises(WebSocketDisconnect) as e:
            with plain.websocket_connect("/ws") as ws:
                ws.receive_json()
        assert e.value.code == 4423


def test_bearer_api_clients_skip_the_app_lock(c):
    c.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM)
    lock(c)
    other = TestClient(c.app, base_url=ORIGIN)
    assert other.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def enroll(c, auth):
    assert c.post("/api/webauthn/register/options").status_code == 403  # step-up required
    opts = c.post("/api/webauthn/register/options", headers=CONFIRM).json()
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert opts["authenticatorSelection"]["authenticatorAttachment"] == "platform"
    r = c.post("/api/webauthn/register/verify", json={"credential": auth.create(opts), "label": "iPhone Face ID"})
    assert r.status_code == 200, r.text


def test_face_id_enrol_and_unlock(c):
    auth = SoftAuthenticator()
    enroll(c, auth)
    a = c.get("/api/auth").json()
    assert a["lock"]["faceid"] is True
    lock(c)
    assert c.get("/api/state").status_code == 423
    opts = c.post("/api/webauthn/auth/options").json()
    assert opts["userVerification"] == "required"
    assert c.post("/api/webauthn/auth/verify", json={"credential": auth.get(opts)}).status_code == 200
    assert c.get("/api/state").status_code == 200
    assert any(e["detail"] == "unlocked with iPhone Face ID" for e in c.desk.security.events)


def test_face_id_rejects_replay_wrong_origin_and_no_face_check(c):
    auth = SoftAuthenticator()
    enroll(c, auth)
    lock(c)
    opts = c.post("/api/webauthn/auth/options").json()
    good = auth.get(opts)
    assert c.post("/api/webauthn/auth/verify", json={"credential": good}).status_code == 200
    lock(c)
    assert c.post("/api/webauthn/auth/verify", json={"credential": good}).status_code == 403  # replayed
    opts = c.post("/api/webauthn/auth/options").json()
    phish = auth.get(opts, origin="https://tradezone-login.evil")
    assert c.post("/api/webauthn/auth/verify", json={"credential": phish}).status_code == 403  # phishing site
    auth.uv = False  # authenticator says "present" but the face was not verified
    opts = c.post("/api/webauthn/auth/options").json()
    assert c.post("/api/webauthn/auth/verify", json={"credential": auth.get(opts)}).status_code == 403
    assert c.get("/api/state").status_code == 423  # still locked


def test_face_id_from_another_device_key_rejected(c):
    enroll(c, SoftAuthenticator())
    lock(c)
    stranger = SoftAuthenticator()
    stranger.cred_id = b"\x02" * 16
    opts = c.post("/api/webauthn/auth/options").json()
    assert c.post("/api/webauthn/auth/verify", json={"credential": stranger.get(opts)}).status_code == 403


def test_remove_face_id_key(c):
    enroll(c, SoftAuthenticator())
    c.app.state.expire_confirmations()  # adding Face ID just proved it's the owner; check the step-up itself
    keys = c.get("/api/applock").json()["passkeys"]
    assert c.delete(f"/api/webauthn/passkeys/{keys[0]['id']}").status_code == 403  # step-up
    assert c.delete(f"/api/webauthn/passkeys/{keys[0]['id']}", headers=CONFIRM).status_code == 409  # only way in
    c.post("/api/applock/pin", json={"pin": "482915"}, headers=CONFIRM)
    assert c.delete(f"/api/webauthn/passkeys/{keys[0]['id']}", headers=CONFIRM).status_code == 200
    assert c.get("/api/applock").json()["passkeys"] == []
