"""The Stacker: slowly builds a long-term hold of SOL, XRP, DOGE and ETH on the side - never sold, no stop-loss."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_kraken import FakeKraken, broker
from tradezone.config import Settings
from tradezone.core.models import Snapshot, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

SYMS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"]
PX = {"BTC-USD": 60_000.0, "ETH-USD": 3_000.0, "SOL-USD": 150.0, "XRP-USD": 0.6, "DOGE-USD": 0.2}


def make(cash=1_000.0, db=":memory:", **kw):
    s = Settings(feed="sim", symbols=list(SYMS), starting_cash=cash, wide_stop=False, **kw)
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    for sym, px in PX.items():
        prime(desk, sym, px)
    return desk


def tick(desk, ts, rsi=45):
    for sym, px in PX.items():
        desk.bus.publish("market.tick", Tick(sym, px, 1000, ts))
    run(desk.bus.drain())
    for sym, px in PX.items():  # (after the team has read the tick: these are the readings the Stacker sees)
        snap = desk.bots["risk"].snapshots[sym]
        desk.bots["risk"].snapshots[sym] = Snapshot(sym, px, {**snap.indicators, "rsi": rsi})


def day(desk, hours=24, rsi=45, t0=1_800_000_000):
    st = desk.bots["stacker"]
    for h in range(int(hours * 4) + 1):  # every 15 minutes
        tick(desk, t0 + h * 900, rsi)
        run(st.on_cycle())
        run(desk.bus.drain())
    return st


def test_slowly_stacks_the_four_coins_on_the_side():
    desk = make()
    pnl = desk.lifetime_pnl()
    st = day(desk, hours=24 * 8)  # 1% of $1,000 a day = $10 a day
    held = st.summary()["holdings"]
    assert {h["symbol"] for h in held} == {"ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"}  # never BTC
    spent = sum(h["cost"] for h in held)
    assert 60 <= spent <= 85  # about $10 a day, a little at a time
    assert desk.portfolio.cash == pytest.approx(1_000.0 - spent)
    assert desk.portfolio.open_positions == []  # not trades: nothing in the trading book
    assert desk.lifetime_pnl() == pytest.approx(pnl)  # moving money into the stack isn't a loss
    assert desk.bots["sentinel"].audit_books() == []
    assert max(h["cost"] for h in held) - min(h["cost"] for h in held) <= 25  # spread evenly


def test_held_coins_are_never_sold_and_have_no_stop():
    desk = make()
    st = day(desk, hours=24 * 3)
    before = {h["symbol"]: h["qty"] for h in st.summary()["holdings"]}
    assert before
    desk.flatten("test")
    desk.halt("test kill", flatten=True)
    PX.update({k: v * 0.5 for k, v in PX.items()})  # a crash: no stop-loss, nothing sold
    try:
        tick(desk, 1_900_000_000)
        run(desk.bus.drain())
        assert {h["symbol"]: h["qty"] for h in st.summary()["holdings"]} == before
        assert st.summary()["pnl"] < 0  # it shows the drop - and keeps holding
    finally:
        PX.update({k: v * 2 for k, v in PX.items()})


def test_waits_while_trading_is_paused_or_stopped():
    desk = make()
    desk.pause("operator")
    st = day(desk, hours=24 * 3)
    assert st.summary()["holdings"] == [] and st.summary()["blocked"] == "trading is paused"


def test_a_share_of_each_winning_trade_goes_into_the_stack_budget():
    desk = make()
    st = desk.bots["stacker"]
    desk.bus.publish("trade.log", {"symbol": "SOL-USD", "pnl": 40.0, "mode": "paper"})
    desk.bus.publish("trade.log", {"symbol": "SOL-USD", "pnl": -10.0, "mode": "paper"})
    run(desk.bus.drain())
    assert st.budget["paper"] == pytest.approx(10.0)  # a quarter of the win; losses take nothing


def test_prefers_coins_that_arent_running_hot():
    desk = make()
    st = desk.bots["stacker"]
    t0 = 1_800_000_000
    for h in range(4 * 24 + 1):
        tick(desk, t0 + h * 900, rsi=80)
        snap = desk.bots["risk"].snapshots["XRP-USD"]
        desk.bots["risk"].snapshots["XRP-USD"] = Snapshot("XRP-USD", 0.6, {**snap.indicators, "rsi": 40})
        run(st.on_cycle())
        if st.history:
            break
    assert st.history[0]["symbol"] == "XRP-USD"  # the one coin that isn't overheated


def test_buys_anyway_after_waiting_hours_for_a_cooler_price():
    desk = make()
    st = day(desk, hours=24, rsi=80)
    assert st.metrics.get("buys", 0) >= 1  # slow and steady matters more than timing


def test_keeps_cash_free_for_trading_and_caps_the_stack():
    desk = make(stack_daily_usd=400.0, stack_max_buy_usd=400.0)
    st = day(desk, hours=24 * 10)
    total = desk.portfolio.equity + st.value()
    assert st.value() <= 0.5 * total + 1  # never past half of everything
    assert desk.free_cash() >= 0.25 * desk.portfolio.equity - 1


def test_the_stack_is_remembered_after_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    desk = make(db=db)
    day(desk, hours=24 * 3)
    held = desk.bots["stacker"].summary()["holdings"]
    desk.bots["stacker"].save()
    again = make(db=db)
    assert again.bots["stacker"].load()
    assert [h["qty"] for h in again.bots["stacker"].summary()["holdings"]] == [h["qty"] for h in held]


def test_real_money_stack_on_kraken_is_left_alone_by_the_records_check():
    fk = FakeKraken(usd=2_000.0)
    fk.price.update({"XBTUSD": 60_000.0, "XDGUSD": 0.2})
    s = Settings(feed="coinbase", live_venue="kraken", kraken_key="KEY", kraken_secret=__import__("tests.test_kraken").test_kraken.SECRET,
                 symbols=["BTC-USD", "DOGE-USD"], stack_coins=["DOGE"], stack_daily_usd=20.0)
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker(fk))
    run(desk.live_broker.refresh_market())
    desk.owner_protected = lambda: True
    run(desk.switch_mode("live"))
    for sym, px in (("BTC-USD", 60_000.0), ("DOGE-USD", 0.2)):
        prime(desk, sym, px)
    st = desk.bots["stacker"]
    alerts = capture(desk, "alert")
    for h in range(4 * 24 + 1):
        desk.bus.publish("market.tick", Tick("DOGE-USD", 0.2, 1000, 1_800_000_000 + h * 900))
        run(desk.bus.drain())
        run(st.on_cycle())
    held = st.summary()["holdings"]
    assert held and held[0]["symbol"] == "DOGE-USD" and held[0]["qty"] >= 30  # Kraken's minimum: 20 DOGE + 5%
    assert fk.bal["XXDG"] == pytest.approx(held[0]["qty"])  # really bought on Kraken
    pm = desk.bots["portfolio"]
    pm.last_fill_ts = 0
    for _ in range(3):
        assert run(pm.reconcile_positions()) == []  # not "sold outside": it's the hold stack
        pm.sync_cash(run(desk.live_broker.account()))
    assert desk.portfolio.cash == pytest.approx(fk.bal["ZUSD"])  # balances still match to the cent
    assert not any("more DOGE" in a.message or "taken out" in a.message for a in alerts)
    assert desk.portfolio.open_positions == [] and "Withdraw" not in fk.calls


def test_pausing_stacking_from_the_dashboard_keeps_what_is_held_and_survives_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    desk = make(db=db)
    st = day(desk, hours=24 * 2)
    n = st.metrics.get("buys", 0)
    desk.set_bot_enabled("stacker", False)
    assert st.enabled  # (an order in flight is still followed to the end)
    day(desk, hours=24 * 2, t0=1_800_500_000)
    assert st.metrics.get("buys", 0) == n and st.summary()["on"] is False and st.summary()["holdings"]
    again = make(db=db)
    again.bots["stacker"].load()
    assert again.settings.stack is False
