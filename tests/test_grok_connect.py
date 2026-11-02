"""Grok connection problems are explained, self-healed where possible, and fixable from the dashboard."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tradezone.bots.grok import GrokClient, pick_model
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app
from tests.test_bots import capture, run

KEY = "xai-" + "a" * 60
TOKEN = "correct-horse-battery-staple-42"


class FakeXAI:
    def __init__(self, models=("grok-4", "grok-3"), key=KEY, status=None):
        self.models, self.key, self.status, self.calls = list(models), key, status, []

    def __call__(self, req: httpx.Request):
        self.calls.append((req.method, req.url.path, req.headers.get("authorization")))
        if self.status == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if self.status == "credits":
            return httpx.Response(403, json={"error": "Your team has no credits left"})
        if req.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": "Incorrect API key provided"})
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        model = json.loads(req.content)["model"]
        if model not in self.models:
            return httpx.Response(404, json={"error": f"The model {model} does not exist or your team does not have access to it."})
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeXAI(**kw)
        monkeypatch.setattr(GrokClient, "transport", httpx.MockTransport(f))
        return f
    return install


def desk_with(key=KEY, **kw):
    return TradingDesk(Settings(feed="sim", grok_api_key=key, **kw), journal=Journal(":memory:"))


def test_pick_model():
    assert pick_model(["grok-3", "grok-4-latest", "grok-4", "grok-2-image"]) == "grok-4-latest"
    assert pick_model(["grok-3-mini", "grok-3", "grok-2-vision"]) == "grok-3"
    assert pick_model(["grok-4", "grok-3"], "grok-3") == "grok-3"
    assert pick_model(["grok-2-image"]) is None


def test_connects(fake):
    fake()
    d = desk_with()
    r = run(d.bots["grok"].ping())
    assert r["ok"] and r["model"] == "grok-4" and d.bots["grok"].connected is True


def test_wrong_key_explained(fake):
    fake()
    d = desk_with(key="xai-wrong-key-123456789012345")
    alerts = capture(d, "alert")
    r = run(d.bots["grok"].ping())
    run(d.bus.drain())
    assert not r["ok"] and r["kind"] == "auth" and "console.x.ai" in r["error"]
    assert "new trades are on hold" in alerts[-1].message.lower()


def test_key_with_stray_whitespace_still_works(fake):
    f = fake()
    d = desk_with(key=f"  {KEY}\n")
    assert run(d.bots["grok"].ping())["ok"]
    assert f.calls[-1][2] == f"Bearer {KEY}"


def test_unavailable_model_switches_automatically(fake):
    fake(models=["grok-3", "grok-3-mini", "grok-2-image"])
    d = desk_with(grok_model="grok-4")
    r = run(d.bots["grok"].ping())
    assert r["ok"] and r["model"] == "grok-3"


def test_no_credits_and_timeouts_explained(fake):
    fake(status="credits")
    r = run(desk_with().bots["grok"].ping())
    assert r["kind"] == "credits" and "credits" in r["error"]
    fake(status="timeout")
    r = run(desk_with().bots["grok"].ping())
    assert r["kind"] == "network"


def test_health_check_runs_from_the_cycle(fake):
    f = fake()
    d = desk_with()
    run(d.bots["grok"].on_cycle())
    run(d.bots["grok"].wait_idle())
    assert d.bots["grok"].connected is True and f.calls


def test_set_key_from_dashboard_never_leaks_and_survives_restart(fake, tmp_path):
    fake()
    db = str(tmp_path / "tz.db")
    desk = TradingDesk(Settings(feed="sim", api_token=TOKEN), journal=Journal(db))
    with TestClient(create_app(desk, run_desk=False), base_url="https://testserver") as c:
        c.post("/api/login", json={"token": TOKEN})
        assert c.get("/api/grok").json()["configured"] is False
        assert c.post("/api/grok/config", json={"api_key": KEY}).status_code == 403  # needs the access token
        assert c.post("/api/grok/config", json={"api_key": "short"}, headers={"X-TZ-Confirm": TOKEN}).status_code == 400
        r = c.post("/api/grok/config", json={"api_key": KEY, "mode": "veto"}, headers={"X-TZ-Confirm": TOKEN}).json()
        assert r["ok"] and r["connected"] and r["source"] == "dashboard" and r["key"] == "…" + KEY[-4:]
        for path in ("/api/grok", "/api/state"):
            assert KEY not in c.get(path).text  # the key never goes back to the browser
    again = TradingDesk(Settings(feed="sim", api_token=TOKEN), journal=Journal(db))
    run(again.start())
    assert again.bots["grok"].info()["source"] == "dashboard"
    assert run(again.bots["grok"].ping())["ok"]


def test_switch_to_advisory_unblocks_trading(fake):
    fake()
    d = desk_with(key="xai-wrong-key-123456789012345")
    g = d.bots["grok"]
    run(g.ping())
    from tradezone.core.models import Side, Signal
    sig = Signal("AAPL", Side.BUY, 0.9, "t", "quant")
    assert g.gates(sig)  # veto + broken = holds entries
    g.save(None, None, "advisory")
    assert not g.gates(sig)
