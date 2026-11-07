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


def test_overlapping_kraken_calls_never_send_nonces_out_of_order():
    """The balance check and the bots' orders can overlap: Kraken must see the nonces in order."""
    import asyncio

    from tradezone.brokers.kraken import KrakenBroker
    seen, sent = [], []
    fk = FakeKraken()

    async def slow(req):
        import urllib.parse
        if "/private/" not in req.url.path:
            return fk.handler(req)
        n = int(dict(urllib.parse.parse_qsl(req.content.decode()))["nonce"])
        sent.append(n)
        await asyncio.sleep(0.02 if len(sent) == 1 else 0)  # the first call is slow to arrive
        seen.append(n)
        return fk.handler(req)

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            await req.aread()
            return await slow(req)

    b = KrakenBroker("KEY-A", SECRET, client=httpx.AsyncClient(base_url="https://api.kraken.com", transport=T()))

    async def both():
        await asyncio.gather(b.account(), b.account(), b.positions())
    run(both())
    assert seen == sorted(seen) and len(seen) >= 3


def test_an_accounts_warnings_show_in_the_main_alerts(exchange):
    from tradezone.core.models import Alert
    desk = main_desk()
    acct_id = run(desk.fleet.add("kraken", "KEY-B", SECRET, "Two"))["id"]
    d = desk.fleet.desk(acct_id)
    d.bus.publish("alert", Alert("critical", "daily loss limit hit", "risk"))
    d.bus.publish("alert", Alert("info", "just chatter", "desk"))
    run(d.bus.drain())
    run(desk.bus.drain())
    msgs = [a["payload"]["message"] for a in desk.bots["sentinel"].alerts]
    assert "[Two] daily loss limit hit" in msgs and not any("chatter" in m for m in msgs)
    assert not desk.halted and not desk.paused  # only reported: the main account isn't affected


def test_balance_checks_run_in_the_background(exchange):
    desk = main_desk()
    acct_id = run(desk.fleet.add("kraken", "KEY-B", SECRET))["id"]
    desk.fleet.balance_at.clear()

    async def go():
        desk.fleet.refresh_soon()
        task = desk.fleet._refresh
        assert task is not None and not task.done()  # (the cycle carries on meanwhile)
        desk.fleet.refresh_soon()
        assert desk.fleet._refresh is task  # never two at once
        await task
    run(go())
    assert desk.fleet.balances[acct_id]["cash"] == 1_200.0


def test_the_risk_level_applies_to_every_linked_account(exchange):
    from fastapi.testclient import TestClient

    from tradezone.server import create_app
    desk = main_desk()
    acct_id = run(desk.fleet.add("kraken", "KEY-B", SECRET))["id"]
    with TestClient(create_app(desk, run_desk=False)) as c:
        assert c.post("/api/risk-profile", json={"profile": "bold", "confirm": "RISK"}).status_code == 200
    assert desk.settings.risk_profile == desk.fleet.desk(acct_id).settings.risk_profile == "bold"


def test_no_mode_switch_while_a_hold_stack_purchase_is_filling(exchange):
    desk = main_desk()
    desk.bots["stacker"].pending = {"order": None}
    assert any("hold-stack purchase" in b for b in desk._switch_blockers("live"))


def test_settings_for_each_venue_point_at_its_own_prices_and_keys():
    from tradezone.desk import TradingDesk as TD
    main = TD(Settings(feed="coinbase", symbols=list(SYMS)), feed=SimulatedFeed(SYMS), journal=Journal(":memory:"))
    f = main.fleet
    k = f._settings("kraken", {"key": "K", "secret": "S"})
    a = f._settings("alpaca", {"key": "AK1", "secret": "S"})
    u = f._settings("uphold", {"token": "t" * 30})
    assert (k.feed, k.kraken_key, k.live_venue) == ("kraken", "K", "kraken")
    assert (a.feed, a.alpaca_live_key, a.alpaca_key) == ("alpaca", "AK1", "")  # never the main practice keys
    assert (u.feed, u.uphold_token) == ("coinbase", "t" * 30)
    assert k.symbols == [] and k.journal_path == ""  # its own coin list and its own database


def test_unknown_venues_and_too_many_accounts_are_refused(exchange):
    desk = main_desk()
    with pytest.raises(FleetError, match="Choose Kraken, Alpaca or Uphold"):
        run(desk.fleet.add("tastyfx", "x", "y"))
    import tradezone.fleet as fleet_mod
    old = fleet_mod.MAX_EXTRA
    fleet_mod.MAX_EXTRA = 1
    try:
        run(desk.fleet.add("kraken", "KEY-A", SECRET))
        with pytest.raises(FleetError, match="Up to 1 extra"):
            run(desk.fleet.add("kraken", "KEY-B", SECRET))
    finally:
        fleet_mod.MAX_EXTRA = old


def test_extra_accounts_run_their_own_teams_and_stop_cleanly(exchange):
    import asyncio
    desk = main_desk()
    acct_id = run(desk.fleet.add("kraken", "KEY-A", SECRET))["id"]
    d = desk.fleet.desk(acct_id)
    d.settings.cycle_seconds = 0.01

    async def go():
        await desk.fleet.start()
        await asyncio.sleep(0.3)
        running = d.running and d.cycles > 0
        await desk.fleet.stop()
        return running
    assert run(go()) and not d.running and desk.fleet.tasks == {}


def test_pause_and_resume_every_account_at_once(exchange):
    desk = main_desk()
    acct_id = run(desk.fleet.add("kraken", "KEY-A", SECRET))["id"]
    assert desk.fleet.control_all("pause") == 2 and desk.paused and desk.fleet.desk(acct_id).paused
    desk.fleet.control_all("resume")
    assert not desk.paused and not desk.fleet.desk(acct_id).paused
    with pytest.raises(FleetError):
        desk.fleet.control_all("dance")


def test_an_exchange_failing_during_the_balance_check_is_only_logged(exchange):
    desk = main_desk()
    acct_id = run(desk.fleet.add("kraken", "KEY-A", SECRET))["id"]

    async def boom():
        raise RuntimeError("exchange down")
    desk.fleet.desk(acct_id).live_broker.account = boom
    run(desk.fleet.refresh_balances(force=True))  # no exception
    assert desk.fleet.row(acct_id)["exchange"]["cash"] == 500.0  # the last good figure stays


def test_a_damaged_saved_account_is_reported_not_fatal(exchange):
    desk = main_desk()
    desk._save_account_row("extra:bad", {"id": "bad", "venue": "nope", "label": "Broken"})
    alerts = []
    desk.bus.subscribe("alert", lambda e: alerts.append(e.payload), owner="t")
    desk.fleet.load()
    run(desk.bus.drain())
    assert "bad" not in desk.fleet.desks and any("Broken" in a.message for a in alerts)


def test_every_open_trade_of_every_account_is_listed_in_full(exchange):
    """The Open tab lists all of them: each account's trades, with stop, target and age, none cut off."""
    from tradezone.core.portfolio import Position
    desk = main_desk()
    acct = run(desk.fleet.add("kraken", "KEY-A", SECRET, "Kraken savings"))["id"]
    d = desk.fleet.desk(acct)
    for i in range(25):  # more than the old cap of 20
        d.portfolio.positions[f"C{i}-USD"] = Position(symbol=f"C{i}-USD", qty=2.0, avg_price=10.0, last_price=9.0,
                                                      stop_price=6.7, take_profit=11.0, opened_ts=1_700_000_000.0)
    desk.portfolio.positions["BTC-USD"] = Position(symbol="BTC-USD", qty=0.01, avg_price=60_000.0, last_price=61_000.0)
    rows = {r["id"]: r for r in desk.fleet.summary()}
    assert len(rows[acct]["positions"]) == 25 and rows[acct]["open"] == 25
    t = rows[acct]["positions"][0]
    assert (t["avg_price"], t["last_price"], t["stop_price"], t["take_profit"], t["opened_ts"]) == (10.0, 9.0, 6.7, 11.0, 1_700_000_000.0)
    assert t["unrealized_pnl"] == -2.0 and t["market_value"] == 18.0
    assert t["net"] < -2.0  # after the fees of selling it: what never-sell-at-a-loss goes by
    assert rows[acct]["never_sell"] is True
    assert [p["symbol"] for p in rows["main"]["positions"]] == ["BTC-USD"]


def test_alpaca_is_unlinked_once_on_request_and_can_be_linked_again(tmp_path):
    from tests.helpers import capture
    db = str(tmp_path / "tz.db")
    d = main_desk(db)
    d._save_account_row("alpaca_live", {"key": "AKXXXXXXXX", "secret": "S"})
    alerts = capture(d, "alert")
    run(d.start())
    assert d._account_row("alpaca_live") is None and d.live_broker is None
    assert not d.settings.alpaca_live_key and not d.broker_info()["linked"]
    assert any("Alpaca unlinked as you asked" in a.message and "untouched" in a.message for a in alerts)
    run(d.shutdown())
    d2 = main_desk(db)
    d2._save_account_row("alpaca_live", {"key": "AKYYYYYYYY", "secret": "S"})  # linked again later...
    d2._unlink_alpaca_once()
    assert d2._account_row("alpaca_live") is not None  # ...and kept


def test_a_linked_alpaca_account_is_unlinked_once_and_kraken_ones_stay(exchange):
    from tests.helpers import capture
    desk = main_desk()
    kraken = run(desk.fleet.add("kraken", "KEY-B", SECRET, "Kraken two"))["id"]
    s = Settings(feed="sim", symbols=list(SYMS))
    alp = TradingDesk(s, feed=SimulatedFeed(SYMS), journal=Journal(":memory:"))
    desk.fleet.desks["alp1"], desk.fleet.meta["alp1"] = alp, {"id": "alp1", "venue": "alpaca", "label": "Alpaca stocks"}
    desk._save_account_row("extra:alp1", desk.fleet.meta["alp1"])
    alerts = capture(desk, "alert")
    run(desk.fleet._unlink_alpaca_once())
    run(desk.bus.drain())
    assert "alp1" not in desk.fleet.desks and desk._account_row("extra:alp1") is None and kraken in desk.fleet.desks
    assert any(a.message.startswith("Alpaca stocks: Alpaca unlinked") for a in alerts)
    desk.fleet.desks["alp2"], desk.fleet.meta["alp2"] = alp, {"id": "alp2", "venue": "alpaca", "label": "again"}
    run(desk.fleet._unlink_alpaca_once())
    assert "alp2" in desk.fleet.desks  # linked again later: kept
