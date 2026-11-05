"""Dip Buyer: buy more of a coin held at a loss once it dips further and turns up (lower average price)."""

from tests.helpers import capture, run
from tests.test_patient import bar
from tests.test_quick_flips import buy
from tests.test_quick_flips import make as _make


def make(**kw):
    kw.setdefault("patient_exits", True)
    return _make(**kw)


def dip(desk, prices, rsi=30.0):
    for px in prices:
        bar(desk, "BTC-USD", px, mom=-0.2)
        snap = desk.bots["risk"].snapshots["BTC-USD"]
        snap.indicators["rsi"] = rsi
        # re-send with the oversold reading the Dip Buyer looks for
        desk.bus.publish("market.snapshot", snap)
        run(desk.bus.drain())


def test_buys_the_dip_on_a_held_loser_when_it_turns_up_lowering_the_average():
    desk = make()
    pos = buy(desk)
    q0, avg0 = pos.qty, pos.avg_price
    dip(desk, [47_000.0, 46_000.0, 46_300.0])  # -8%, then turning up
    assert pos.qty > q0 and pos.avg_price < avg0
    assert desk.bots["dipbuyer"].metrics["dip_ideas"] == 1


def test_not_while_it_is_still_falling_or_not_far_enough_down():
    desk = make()
    pos = buy(desk)
    q0 = pos.qty
    dip(desk, [49_000.0, 48_500.0, 48_700.0])  # only -2.6%: not a dip worth adding to
    dip(desk, [46_000.0, 45_500.0])  # falling, not turning
    assert pos.qty == q0


def test_at_most_twice_per_trade_and_never_past_the_biggest_position():
    desk = make()
    pos = buy(desk)
    for low in (46_000.0, 42_000.0, 38_000.0, 34_000.0):
        dip(desk, [low, low * 1.01])
    eq = desk.portfolio.equity
    assert desk.bots["dipbuyer"].adds["BTC-USD"][1] <= 2
    assert pos.qty * pos.last_price <= eq * desk.settings.limits(True).max_position_pct * 1.01


def test_keeps_a_quarter_of_the_balance_in_cash():
    desk = make()
    pos = buy(desk)
    desk.portfolio.cash = desk.settings.dip_keep_cash * desk.portfolio.equity + 50.0  # little spare cash left
    q0 = pos.qty
    dip(desk, [46_000.0, 46_300.0])
    spent = (pos.qty - q0) * 46_300.0
    assert spent <= 50.0 * 1.01  # only the spare above the quarter kept in cash


def test_waits_while_trading_is_stopped_and_only_runs_with_never_sell_on():
    desk = make()
    pos = buy(desk)
    q0 = pos.qty
    desk.halt("test", flatten=False)
    dip(desk, [46_000.0, 46_300.0])
    assert pos.qty == q0
    desk2 = make(never_sell_at_loss=False)
    p2 = buy(desk2)
    q2 = p2.qty
    desk2.settings.wide_stop_pct = 0.9  # (so its stop doesn't sell it first)
    dip(desk2, [46_000.0, 46_300.0])
    assert p2.is_flat or p2.qty == q2  # cut by the rules, never added to


def test_a_flash_crash_blocks_it():
    desk = make()
    pos = buy(desk)
    q0 = pos.qty
    desk.bots["shockguard"].until = 10**12  # a market-wide shock in progress
    rejected = capture(desk, "signal.rejected")
    dip(desk, [46_000.0, 46_300.0])
    assert pos.qty == q0 and any("Flash-Crash" in r["reason"] for r in rejected)
