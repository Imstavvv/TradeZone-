"""The whole Kraken balance trades (asked for): your own coins there are taken over by the bots as trades."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_kraken import SECRET, FakeKraken, broker
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def live_desk(fk, **kw):
    s = Settings(feed="coinbase", live_venue="kraken", kraken_key="KEY", kraken_secret=SECRET,
                 symbols=["BTC-USD", "DOGE-USD"], **kw)
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker(fk))
    run(d.live_broker.refresh_market())
    d.owner_protected = lambda: True
    for sym, px in (("BTC-USD", 60_000.0), ("DOGE-USD", 0.2)):
        prime(d, sym, px)
    run(d.switch_mode("live"))
    return d


def cycle(d):
    pm = d.bots["portfolio"]
    pm.last_fill_ts = 0
    for _ in range(2):
        pm._cycles = pm.RECONCILE_EVERY - 1
        run(pm.on_cycle())
        run(d.bus.drain())
    return pm


def test_your_kraken_coins_become_trades_with_the_stop_and_target_and_the_books_still_match():
    fk = FakeKraken(usd=1_000.0, held={"XXBT": 0.01, "ETH2.S": 1.5})  # BTC from before going live, staked ETH
    d = live_desk(fk)
    assert d.live_broker.baseline.get("XXBT") == 0.01  # (noted as yours when going live)
    alerts = capture(d, "alert")
    pm = cycle(d)
    pos = d.portfolio.position("BTC-USD")
    assert pos.qty == pytest.approx(0.01)  # taken over...
    assert pos.stop_price == pytest.approx(60_000 * 0.93, rel=1e-3)  # ...with the -7% stop...
    assert pos.take_profit == pytest.approx(60_000 * 1.15 + d.settings.round_trip_cost("BTC-USD", 60_000), rel=1e-3)
    assert d.portfolio.cash == pytest.approx(1_000.0)  # (no money moved; the coins' value joined the trading)
    assert "ETH2.S" in fk.bal and fk.bal["XXBT"] == 0.01 and "AddOrder" not in fk.calls  # nothing sent or staked touched
    assert any("took over your Kraken coins" in a.message for a in alerts)
    for _ in range(3):  # the records' check stays clean: nothing "sold outside" or "yours"
        assert run(pm.reconcile_positions()) == []
    assert pm.outside == {} and d.live_broker.baseline.get("XXBT", 0.0) == 0.0


def test_the_hold_stack_and_a_coin_already_traded_are_left_alone():
    fk = FakeKraken(usd=1_000.0, held={"XXDG": 300.0})
    d = live_desk(fk, stack_coins=["DOGE"])
    st = d.bots["stacker"]
    st.books["live"]["DOGE-USD"] = {"qty": 300.0, "cost": 60.0}  # the hold stack holds all of it
    cycle(d)
    assert d.portfolio.position("DOGE-USD").is_flat and st.books["live"]["DOGE-USD"]["qty"] == 300.0


def test_it_can_be_switched_off_and_never_runs_in_practice():
    fk = FakeKraken(usd=1_000.0, held={"XXBT": 0.01})
    d = live_desk(fk, adopt_own_coins=False)
    cycle(d)
    assert d.portfolio.position("BTC-USD").is_flat
