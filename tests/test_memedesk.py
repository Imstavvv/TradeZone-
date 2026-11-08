"""The MemeCoin desk attached read-only (asked for): its status shown in TradeZone, from a public https address only."""

import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import run
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.memedesk import MemeDesk, check_url
from tradezone.server import create_app


@pytest.mark.parametrize("bad", ["http://memecoinz.onrender.com", "https://localhost:8000", "https://127.0.0.1",
                                 "https://10.0.0.5", "https://192.168.1.20", "https://desk.internal", "ftp://x.com",
                                 "https://user:pw@memecoinz.onrender.com"])
def test_only_a_public_https_address_is_accepted(bad):
    with pytest.raises(ValueError):
        check_url(bad)


def test_a_public_address_is_cleaned_up():
    assert check_url(" https://memecoinz.onrender.com/api/state ") == "https://memecoinz.onrender.com"


def real_state():
    """The MemeCoin desk's own /api/state, from its own code (a simulated market) when it's checked out next door."""
    sys.path.insert(0, "/home/user/memecoinz-")
    try:
        from desk.config import Config
        from desk.server import DeskService
    except ImportError:
        pytest.skip("the MemeCoinz repository isn't checked out next to TradeZone")
    finally:
        sys.path.remove("/home/user/memecoinz-")
    import tempfile
    svc = DeskService(Config(), "sim", tempfile.mktemp(suffix=".sqlite"))
    import threading
    svc.loop(threading.Event(), max_steps=10)
    return svc.state()


def test_its_real_status_is_read_and_shown():
    state = real_state()
    md = MemeDesk()
    md.url = "https://memecoinz.onrender.com"
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=json.dumps(state))))
    run(md.refresh())
    v = md.view()
    assert v["status"] == "running" and v["equity"] == state["equity"] and v["error"] == ""
    assert v["closed_trades"] == state["closed_trades"] and isinstance(v["positions"], list)
    run(md.close())


def test_an_unreachable_desk_is_said_and_trading_carries_on():
    md = MemeDesk()
    md.url = "https://memecoinz.onrender.com"
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(502, text="bad gateway")))
    run(md.refresh())
    v = md.view()
    assert v["status"] == "unreachable" and "502" in v["error"] and v["equity"] is None
    run(md.close())


def test_attach_and_detach_from_settings():
    d = TradingDesk(Settings(feed="sim", symbols=["BTC-USD"]), feed=SimulatedFeed(["BTC-USD"]), journal=Journal(":memory:"))
    with TestClient(create_app(d, run_desk=False)) as c:
        assert c.post("/api/memedesk", json={"url": "http://not-https.example"}).status_code == 400
        d.memedesk.refresh = lambda: _noop()  # (no network in tests)
        r = c.post("/api/memedesk", json={"url": "https://memecoinz.onrender.com"})
        assert r.status_code == 200 and r.json()["memedesk"]["url"] == "https://memecoinz.onrender.com"
        assert d.store.load("memedesk") == {"url": "https://memecoinz.onrender.com"}
        assert d.state()["memedesk"]["url"] == "https://memecoinz.onrender.com"
        assert c.post("/api/memedesk", json={"url": ""}).json()["memedesk"] is None  # detached
    assert d.state()["memedesk"] is None


async def _noop():
    return None
