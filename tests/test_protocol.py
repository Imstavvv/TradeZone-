"""The make-me-money protocol: the safest proven path, and a one-tap reset to the tested-best settings."""

from fastapi.testclient import TestClient

from tests.helpers import run
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def make(db=":memory:"):
    s = Settings(feed="sim", symbols=["BTC-USD", "SOL-USD"])
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))


def test_a_new_install_is_already_on_the_tested_best_settings():
    st = make().protocol_status()
    assert st["off"] == [] and [x["key"] for x in st["steps"]] == ["practice", "link", "small", "best", "review", "cashout"]


def test_drifted_settings_are_listed_and_one_tap_puts_them_back(tmp_path):
    db = str(tmp_path / "tz.db")
    desk = make(db)
    desk.set_strategy("vwap", True)
    desk.set_strategy("pullback", False)
    desk.set_never_sell(False)
    desk.settings.flip_max_skims = 4
    off = {x["key"] for x in desk.protocol_status()["off"]}
    assert off == {"strategies", "pullback", "never_sell_at_loss", "flip_max_skims"}
    with TestClient(create_app(desk, run_desk=False)) as c:
        r = c.post("/api/protocol", json={"action": "best"})
        assert r.status_code == 200 and r.json()["off"] == []
    d2 = make(db)  # remembered after a restart
    run(d2.start())
    assert d2.protocol_status()["off"] == []
    run(d2.shutdown())


def test_the_risk_level_and_cap_stay_yours_and_the_cap_is_remembered(tmp_path):
    db = str(tmp_path / "tz.db")
    desk = make(db)
    desk.set_risk_profile("aggressive", "RISK")
    with TestClient(create_app(desk, run_desk=False)) as c:
        assert c.post("/api/protocol", json={"action": "cap", "amount": 50}).json()["cap"] == 50
        assert c.post("/api/protocol", json={"action": "cap", "amount": -5}).status_code == 400
        c.post("/api/protocol", json={"action": "best"})
    assert desk.settings.risk_profile == "aggressive" and desk.settings.live_max_capital == 50
    d2 = make(db)
    run(d2.start())
    assert d2.settings.live_max_capital == 50
    run(d2.shutdown())


def test_the_cap_applies_to_every_linked_account(monkeypatch):
    from tests.test_fleet import SECRET, main_desk
    from tests.test_kraken import FakeKraken
    import httpx
    import tradezone.brokers.kraken as kmod
    fk = FakeKraken(usd=900.0)
    real = httpx.AsyncClient
    monkeypatch.setattr(kmod.httpx, "AsyncClient",
                        lambda **kw: real(base_url="https://api.kraken.com", transport=httpx.MockTransport(fk.handler)))
    desk = main_desk()
    acct = run(desk.fleet.add("kraken", "KEY-X", SECRET))["id"]
    with TestClient(create_app(desk, run_desk=False)) as c:
        c.post("/api/protocol", json={"action": "cap", "amount": 75})
    assert desk.settings.live_max_capital == desk.fleet.desk(acct).settings.live_max_capital == 75
