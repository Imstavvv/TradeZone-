"""Several real-money accounts at once: each has its own bot team, its own balance and its own LIVE switch."""

import httpx
import pytest

from tests.helpers import run
from tests.test_kraken import SECRET, FakeKraken
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.fleet import FleetError
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

SYMS = ["BTC-USD", "DOGE-USD"]


@pytest.fixture
def exchange(monkeypatch):
    """Two separate Kraken accounts (by API key), each with its own money."""
    accounts = {"KEY-A": FakeKraken(usd=500.0), "KEY-B": FakeKraken(usd=1_200.0), "KEY-W": FakeKraken(can_withdraw=True)}

    def handler(req):
        return accounts.get(req.headers.get("API-Key"), accounts["KEY-A"]).handler(req)

    import tradezone.brokers.kraken as kmod
    real = httpx.AsyncClient
    monkeypatch.setattr(kmod.httpx, "AsyncClient",
                        lambda **kw: real(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)))
    return accounts


def main_desk(db=":memory:"):
    s = Settings(feed="coinbase", symbols=list(SYMS), withdrawal_cooldown_h=0)
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    real = d.fleet.make_desk

    def make(settings, journal):  # no network in tests: simulated prices, the same coins
        settings.symbols = list(SYMS)
        settings.feed = settings.feed_choice = "coinbase"
        desk = TradingDesk(settings, feed=SimulatedFeed(SYMS), journal=journal)
        desk.security = None
        return desk

    d.fleet.make_desk = make
    assert real
    return d


def test_link_two_more_accounts_each_with_its_own_team(exchange):
    desk = main_desk()
    a = run(desk.fleet.add("kraken", "KEY-A", SECRET, "Kraken savings"))
    b = run(desk.fleet.add("kraken", "KEY-B", SECRET))
    assert a["label"] == "Kraken savings" and a["account"]["cash"] == 500.0 and b["account"]["cash"] == 1_200.0
    da, db = desk.fleet.desk(a["id"]), desk.fleet.desk(b["id"])
    assert da is not db and da.bots is not db.bots and da.portfolio is not db.portfolio
    assert da.live_broker.name == "kraken" and da.settings.kraken_key == "KEY-A" and db.settings.kraken_key == "KEY-B"
    assert desk.settings.kraken_key == ""  # the main account is untouched
    rows = desk.fleet.summary()
    assert [r["id"] for r in rows] == ["main", a["id"], b["id"]] and rows[0]["main"]


def test_the_same_account_cant_be_linked_twice(exchange):
    desk = main_desk()
    run(desk.fleet.add("kraken", "KEY-A", SECRET))
    with pytest.raises(FleetError, match="already linked"):
        run(desk.fleet.add("kraken", "KEY-A", SECRET))


def test_keys_that_can_withdraw_are_still_refused(exchange):
    desk = main_desk()
    with pytest.raises(ValueError, match="Withdraw Funds"):
        run(desk.fleet.add("kraken", "KEY-W", SECRET))
    assert desk.fleet.desks == {} and len(desk.fleet.summary()) == 1


def test_accounts_trade_live_at_the_same_time_with_separate_balances(exchange):
    desk = main_desk()
    ids = [run(desk.fleet.add("kraken", k, SECRET))["id"] for k in ("KEY-A", "KEY-B")]
    for i in ids:
        d = desk.fleet.desk(i)
        run(d.live_broker.refresh_market())
        d.owner_protected = lambda: True
        run(d.switch_mode("live"))
    rows = {r["id"]: r for r in desk.fleet.summary()}
    assert rows[ids[0]]["mode"] == rows[ids[1]]["mode"] == "live" and rows["main"]["mode"] == "paper"
    assert rows[ids[0]]["cash"] == 500.0 and rows[ids[1]]["cash"] == 1_200.0
    assert desk.fleet.totals() == {"accounts": 3, "live": 2, "live_equity": 1_700.0, "live_pnl_today": 0.0}
    run(desk.fleet.refresh_balances(force=True))
    assert desk.fleet.row(ids[1])["exchange"]["cash"] == 1_200.0  # the exchange's own number, shown alongside
    with pytest.raises(FleetError, match="back to practice"):
        run(desk.fleet.remove(ids[0]))
    assert desk.fleet.control_all("kill") == 3 and all(d.halted for d in [desk, *desk.fleet.desks.values()])
    assert not exchange["KEY-A"].calls.count("Withdraw")


def test_linked_accounts_come_back_after_a_restart(exchange, tmp_path):
    db = str(tmp_path / "tz.db")
    desk = main_desk(db)
    acct_id = run(desk.fleet.add("kraken", "KEY-B", SECRET, "second"))["id"]
    run(desk.fleet.stop())
    again = main_desk(db)
    again.fleet.load()
    d = again.fleet.desk(acct_id)
    assert again.fleet.meta[acct_id]["label"] == "second"
    assert d.settings.kraken_key == "KEY-B" and d.live_broker is not None and d.live_broker.name == "kraken"
    assert (tmp_path / f"tz-account-{acct_id}.db").exists()
    run(again.fleet.remove(acct_id))
    assert again.fleet._rows() == []


def test_accounts_api(exchange):
    from fastapi.testclient import TestClient

    from tradezone.server import create_app
    desk = main_desk()
    c = TestClient(create_app(desk, run_desk=False))
    with c:
        r = c.post("/api/fleet", json={"venue": "kraken", "key": "KEY-B", "secret": SECRET, "label": "Two"})
        assert r.status_code == 200 and r.json()["account"]["cash"] == 1_200.0
        acct_id = r.json()["id"]
        assert c.post("/api/fleet", json={"venue": "kraken", "key": "KEY-B", "secret": SECRET}).status_code == 400
        rows = c.get("/api/fleet").json()["accounts"]
        assert [x["label"] for x in rows] == ["Main (practice only)", "Two"]
        assert [x["id"] for x in c.get("/api/state").json()["accounts"]] == ["main", acct_id]
        assert c.post(f"/api/fleet/{acct_id}/mode", json={"mode": "live", "confirm": "nope"}).status_code == 400
        assert c.post(f"/api/fleet/{acct_id}/control/pause").json()["account"]["paused"] is True
        assert c.post("/api/fleet/nope/control/pause").status_code == 404
        assert c.post("/api/fleet/all/control/kill").json()["accounts"] == 2 and desk.halted
        assert c.delete("/api/fleet/main").status_code == 400
        assert c.delete(f"/api/fleet/{acct_id}").json()["ok"] and len(c.get("/api/fleet").json()["accounts"]) == 1
