"""The real-money balance follows the account: money added or taken out at the exchange is picked up."""

import pytest

from tests.helpers import capture, run
from tests.test_kraken import FakeKraken, kraken_desk


def live(fk, **kw):
    desk = kraken_desk(fk)
    for k, v in kw.items():
        setattr(desk.settings, k, v)
    run(desk.live_broker.refresh_market())
    desk.owner_protected = lambda: True
    run(desk.switch_mode("live"))
    pm = desk.bots["portfolio"]
    pm.last_fill_ts = 0.0
    return desk, pm


def check(desk, pm, times=2):
    for _ in range(times):
        pm._cycles = pm.RECONCILE_EVERY - 1
        run(pm.on_cycle())
        run(desk.bus.drain())


def test_a_deposit_at_the_exchange_is_picked_up_and_not_counted_as_profit():
    fk = FakeKraken(usd=500.0)
    desk, pm = live(fk)
    alerts = capture(desk, "alert")
    pnl_before = desk.lifetime_pnl()
    fk.bal["ZUSD"] += 200.0
    check(desk, pm, 1)
    assert desk.portfolio.cash == 500.0  # seen once: not believed yet
    check(desk, pm, 1)
    assert desk.portfolio.cash == pytest.approx(700.0)
    assert desk.lifetime_pnl() == pytest.approx(pnl_before)  # a deposit isn't profit
    assert desk.portfolio.day_start_equity == pytest.approx(700.0)  # nor does it move today's result
    assert desk.bots["sentinel"].audit_books() == []  # the independent ledger agrees
    assert any("$200.00 added to your Kraken account" in a.message for a in alerts)


def test_a_withdrawal_at_the_exchange_lowers_what_the_bots_may_spend():
    fk = FakeKraken(usd=500.0)
    desk, pm = live(fk)
    fk.bal["ZUSD"] -= 150.0
    check(desk, pm)
    assert desk.portfolio.cash == pytest.approx(350.0) and desk.bots["sentinel"].audit_books() == []


def test_a_one_off_blip_or_rounding_is_ignored():
    fk = FakeKraken(usd=500.0)
    desk, pm = live(fk)
    fk.bal["ZUSD"] += 0.40  # rounding-sized
    check(desk, pm, 3)
    assert desk.portfolio.cash == 500.0
    fk.bal["ZUSD"] += 80.0
    check(desk, pm, 1)
    fk.bal["ZUSD"] -= 80.0  # gone again on the next check: never booked
    check(desk, pm, 2)
    assert desk.portfolio.cash == 500.0


def test_with_a_capital_cap_only_money_taken_out_is_followed():
    fk = FakeKraken(usd=1_000.0)
    desk, pm = live(fk, live_max_capital=400.0)
    assert desk.portfolio.cash == 400.0
    fk.bal["ZUSD"] += 500.0
    check(desk, pm)
    assert desk.portfolio.cash == 400.0  # the cap still holds
    fk.bal["ZUSD"] = 300.0  # now below what the bots were counting on
    check(desk, pm)
    assert desk.portfolio.cash == pytest.approx(300.0)


def test_your_own_coins_on_kraken_dont_raise_an_equity_warning_every_half_minute():
    fk = FakeKraken(usd=500.0, held={"XXBT": 0.5})  # $30,000 of your own bitcoin
    desk, pm = live(fk)
    alerts = capture(desk, "alert")
    check(desk, pm, 4)
    assert not any("ledger equity differs" in a.message for a in alerts)
    assert desk.portfolio.cash == 500.0


def test_paper_mode_never_syncs():
    fk = FakeKraken(usd=500.0)
    desk = kraken_desk(fk)
    pm = desk.bots["portfolio"]
    fk.bal["ZUSD"] += 999
    check(desk, pm)
    assert desk.portfolio.cash == desk.settings.starting_cash
