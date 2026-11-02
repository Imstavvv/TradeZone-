"""Site security: firewall, lockout, sessions, step-up, headers, origin checks."""

import re

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.security import SecurityConfig, SecurityManager
from tradezone.server import create_app

TOKEN = "correct-horse-battery-staple-42"
XRP = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


def client(journal=None, **kw):
    kw.setdefault("api_token", TOKEN)
    desk = TradingDesk(Settings(feed="sim", **kw), journal=journal or Journal(":memory:"))
    return desk, TestClient(create_app(desk, run_desk=False), base_url="https://testserver")


def login(c, token=TOKEN):
    return c.post("/api/login", json={"token": token})


def test_security_headers_and_csp_nonce():
    _, c = client()
    with c:
        r = c.get("/")
        csp = r.headers["content-security-policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        assert f'<script nonce="{nonce}">' in r.text  # only our own script carries the nonce
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "max-age" in r.headers["strict-transport-security"]
        assert c.get("/api/health").headers["cache-control"] == "no-store"
        assert c.get("/api/health").json() == {"ok": True}


def test_api_docs_are_not_public():
    _, c = client()
    with c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404


def test_login_sets_secure_session_cookie_and_logout_revokes():
    _, c = client()
    with c:
        assert c.get("/api/state").status_code == 401
        r = login(c)
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
        assert c.get("/api/state").status_code == 200
        assert len(c.get("/api/security").json()["sessions"]) == 1
        c.post("/api/logout")
        assert c.get("/api/state").status_code == 401


def test_signout_everywhere():
    desk, c = client()
    with c:
        login(c)
        other = TestClient(c.app, base_url="https://testserver")
        login(other)
        assert c.post("/api/security/signout-all").json()["revoked"] == 2
        assert other.get("/api/state").status_code == 401


def test_brute_force_bans_ip_even_for_correct_token_then_unban():
    desk, c = client(login_failures_before_ban=3)
    with c:
        for _ in range(3):
            assert login(c, "guess").status_code == 401
        r = login(c)  # right token, but this IP is now banned
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        assert c.get("/api/health").status_code == 200  # health check is never blocked
        kinds = [e["kind"] for e in desk.security.events]
        assert kinds.count("login_failed") == 3 and "ban" in kinds
        desk.security.unban("testclient")
        assert login(c).status_code == 200


def test_signed_in_device_survives_ban_on_shared_ip():
    _, c = client(login_failures_before_ban=2)
    with c:
        login(c)  # you, already signed in
        attacker = TestClient(c.app, base_url="https://testserver")  # same IP (shared Wi-Fi)
        for _ in range(2):
            login(attacker, "guess")
        assert login(attacker).status_code == 429  # attacker locked out, even with the token
        assert c.get("/api/state").status_code == 200  # you keep working


def test_wrong_bearer_tokens_count_as_failed_logins():
    _, c = client(login_failures_before_ban=2)
    with c:
        for _ in range(2):
            c.get("/api/state", headers={"Authorization": "Bearer nope"})
        assert c.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 429


def test_bans_persist_across_restart_and_escalate(tmp_path):
    j = Journal(str(tmp_path / "tz.db"))
    sec = SecurityManager(SecurityConfig(token=TOKEN, ban_s=60), j)
    first = sec.ban("203.0.113.9", "test")
    again = SecurityManager(SecurityConfig(token=TOKEN, ban_s=60), j)
    assert again.banned_for("203.0.113.9") > 0
    assert again.ban("203.0.113.9", "test") == first * 2  # repeat offender: twice as long


def test_rate_limit():
    _, c = client(rate_limit_per_min=5)
    with c:
        codes = [c.get("/api/auth").status_code for _ in range(7)]
        assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


def test_cross_site_requests_and_websockets_refused():
    _, c = client()
    with c:
        login(c)
        evil = {"Origin": "https://evil.example"}
        assert c.post("/api/control/kill", headers=evil).status_code == 403
        assert c.post("/api/control/pause", headers={"Origin": "https://testserver"}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws", headers=evil) as ws:
                ws.receive_json()
        assert e.value.code == 4403


def test_session_cookie_authorises_websocket():
    desk = TradingDesk(Settings(feed="sim", api_token=TOKEN), journal=Journal(":memory:"))
    c = TestClient(create_app(desk, run_desk=False))  # plain http so the test client sends the cookie on ws
    with c:
        login(c)
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_oversized_request_rejected():
    _, c = client()
    with c:
        login(c)
        r = c.post("/api/control/pause", content=b"x" * 70_000, headers={"content-type": "application/json"})
        assert r.status_code == 413


def test_ip_allowlist_firewall():
    _, c = client(allowed_ips=["10.0.0.0/8"])
    with c:
        assert c.get("/").status_code == 403  # test client isn't on the allowlist
        assert c.get("/api/health").status_code == 200  # platform health checks still pass


def test_money_moves_need_the_token_again():
    desk, c = client(withdrawal_cooldown_h=0)
    with c:
        login(c)
        body = {"chain": "XRP", "address": XRP, "tag": "7"}
        assert c.post("/api/wallets", json=body).status_code == 403  # a stolen session isn't enough
        assert c.post("/api/wallets", json=body, headers={"X-TZ-Confirm": "wrong"}).status_code == 403
        r = c.post("/api/wallets", json=body, headers={"X-TZ-Confirm": TOKEN})
        assert r.status_code == 200
        wd = {"wallet_id": r.json()["id"], "asset": "XRP", "amount": 1}
        desk.prices["XRP-USD"] = 1.0
        assert c.post("/api/withdrawals", json=wd).status_code == 403
        assert c.post("/api/withdrawals", json=wd, headers={"X-TZ-Confirm": TOKEN}).status_code == 200
        actions = [e["detail"] for e in desk.security.events if e["kind"] == "action"]
        assert any("link XRP" in a for a in actions) and any("withdraw" in a for a in actions)


def test_going_live_needs_the_token_again():
    _, c = client()
    with c:
        login(c)
        assert c.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}).status_code == 403


def test_public_site_needs_sign_in_and_rejects_weak_token():
    with pytest.raises(RuntimeError, match="too short"):
        create_app(TradingDesk(Settings(feed="sim", host="0.0.0.0", api_token="short"),
                               journal=Journal(":memory:")), run_desk=False)
    # no token at all is fine now: the site starts in first-time-setup mode, closed to everyone
    desk = TradingDesk(Settings(feed="sim", host="0.0.0.0"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/setup/status").json() == {"owner": False, "auth_required": True, "faceid": False}


def test_local_dev_without_token_still_works():
    _, c = client(api_token="")
    with c:
        assert c.get("/api/state").status_code == 200
        assert login(c, "anything").json()["auth_required"] is False
