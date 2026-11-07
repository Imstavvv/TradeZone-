"""A team whose start fails (the exchange briefly unreachable, a key refused) keeps trying and says why - it used to
stop for good, so the account never updated or traded; a linked account's team that stops is started again."""

import asyncio

from tests.helpers import capture
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def desk():
    s = Settings(feed="sim", symbols=["BTC-USD"], cycle_seconds=0.01)
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    d.START_RETRY_S = (0.01,)
    return d


def test_a_failed_start_is_retried_and_said_then_the_team_runs():
    d = desk()
    alerts = capture(d, "alert")
    real, calls = d.start, {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("kraken unreachable")
        await real()
    d.start = flaky

    async def go():
        task = asyncio.ensure_future(d.run_forever())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if d.cycles > 3:
                break
        d.stop()
        await asyncio.wait_for(task, 5)
    asyncio.run(go())
    assert calls["n"] == 3 and d.cycles > 3 and d.start_error == ""
    said = [a.message for a in alerts if "couldn't start" in a.message]
    assert len(said) == 2 and "kraken unreachable" in said[0]


def test_stopping_while_it_retries_ends_it():
    d = desk()

    async def broken():
        raise ConnectionError("down")
    d.start = broken

    async def go():
        task = asyncio.ensure_future(d.run_forever())
        await asyncio.sleep(0.05)
        d.stop()
        await asyncio.wait_for(task, 5)
    asyncio.run(go())
    assert "down" in d.start_error


def test_a_linked_accounts_team_that_stops_is_started_again():
    main = desk()
    d2 = desk()
    main.fleet.desks["a0"], main.fleet.meta["a0"] = d2, {"id": "a0", "label": "Kraken savings"}
    main.fleet.loaded = True
    main.fleet.RESTART_AFTER_S = 0.01
    alerts = capture(main, "alert")
    runs = {"n": 0}

    async def run_once_then_crash():
        runs["n"] += 1
        if runs["n"] == 1:
            raise RuntimeError("feed task died")
        for _ in range(500):  # (runs until stopped)
            if not d2.running:
                break
            await asyncio.sleep(0.01)
    d2.run_forever = run_once_then_crash

    async def go():
        main.running = True
        main.fleet._run("a0")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if runs["n"] >= 2:
                break
        d2.stop()
        main.running = False
        await asyncio.sleep(0.05)
        await main.bus.drain()
    asyncio.run(go())
    assert runs["n"] == 2
    assert any("Kraken savings stopped" in a.message for a in alerts)


def test_all_cash_an_old_real_money_cap_is_cleared_once_and_a_new_one_is_kept(tmp_path):
    from tests.helpers import run
    from tradezone.persistence import StateStore
    db = str(tmp_path / "tz.db")

    def boot():
        s = Settings(feed="sim", symbols=["BTC-USD"])
        return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    d = boot()
    StateStore(d.journal).save("live_cap", {"usd": 200.0})  # (a cap from an older version)
    run(d.start())
    assert d.settings.live_max_capital == 0.0  # all the cash
    d.set_live_cap(300.0)  # set again on purpose...
    run(d.shutdown())
    d2 = boot()
    run(d2.start())
    assert d2.settings.live_max_capital == 300.0  # ...and kept
    run(d2.shutdown())


def test_each_account_row_carries_the_whole_exchange_balance():
    from tests.helpers import FakeLiveBroker
    d = desk()
    d.live_broker = FakeLiveBroker(cash=500.0)
    d.fleet.balances["main"] = {"cash": 500.0, "equity": 1_750.0, "stablecoins": {"USDC": 25.0}}
    row = d.fleet.row("main")
    assert row["exchange"] == {"cash": 500.0, "equity": 1_750.0, "stablecoins": {"USDC": 25.0}}
