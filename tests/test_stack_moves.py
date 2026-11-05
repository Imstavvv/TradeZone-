"""Moving coins by hand between the hold stack and the trading - the moved-out coins are traded again."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_kraken import SECRET, FakeKraken, broker
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(cash=1_000.0):
    s = Settings(feed="sim", symbols=["BTC-USD", "SOL-USD", "XRP-USD"], starting_cash=cash, wide_stop=False)
    s.risk.symbol_cooldown_s = 0
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    for sym, px in (("BTC-USD", 60_000.0), ("SOL-USD", 150.0), ("XRP-USD", 0.6)):
        prime(desk, sym, px)
    return desk


def stack(desk, sym="SOL-USD", qty=2.0, cost=280.0):
    desk.bots["stacker"].books["paper"][sym] = {"qty": qty, "cost": cost, "buys": 3}


def test_move_out_of_the_hold_stack_and_the_bots_trade_it_again():
    desk = make()
    stack(desk)
    st, p = desk.bots["stacker"], desk.portfolio
    cash, pnl = p.cash, desk.lifetime_pnl()
    res = st.move_out("SOL-USD")
    run(desk.bus.drain())
    pos = p.position("SOL-USD")
    assert res["moved"] == 2.0 and pos.qty == pytest.approx(2.0) and pos.avg_price == pytest.approx(150.0)
    assert pos.stop_price is not None and pos.take_profit is not None  # a normal trade: stop and target
    assert p.cash == pytest.approx(cash) and desk.lifetime_pnl() == pytest.approx(pnl)  # cash unchanged, not "profit"
    assert "SOL-USD" not in st.book and desk.bots["sentinel"].audit_books() == []
    trades = capture(desk, "trade.closed")
    up = Tick("SOL-USD", pos.take_profit * 1.01)
    desk.prices["SOL-USD"] = up.price
    desk.broker.on_prices([up])
    desk.bus.publish("market.tick", up)
    run(desk.bus.drain())
    assert trades and trades[-1].pnl > 0  # the bots took profit on it like any trade


def test_move_half_out():
    desk = make()
    stack(desk)
    desk.bots["stacker"].move_out("SOL-USD", 1.0)
    run(desk.bus.drain())
    assert desk.bots["stacker"].book["SOL-USD"]["qty"] == pytest.approx(1.0)
    assert desk.bots["stacker"].book["SOL-USD"]["cost"] == pytest.approx(140.0)  # half the cost goes with half
    assert desk.portfolio.position("SOL-USD").qty == pytest.approx(1.0)


def test_move_a_trade_into_the_hold_stack_and_it_is_never_sold_by_the_bots():
    desk = make()
    desk.bus.publish("signal.proposed", Signal("SOL-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    p, st = desk.portfolio, desk.bots["stacker"]
    pos = p.position("SOL-USD")
    q, cash = pos.qty, p.cash
    assert q > 0
    st.move_in("SOL-USD")
    run(desk.bus.drain())
    assert pos.is_flat and st.book["SOL-USD"]["qty"] == pytest.approx(q)
    assert p.cash == pytest.approx(cash) and desk.bots["sentinel"].audit_books() == []
    desk.flatten("operator")  # Close all trades: the hold stack isn't a trade
    run(desk.bus.drain())
    assert st.book["SOL-USD"]["qty"] == pytest.approx(q)


def test_moves_are_refused_when_they_cant_be_done_cleanly():
    desk = make()
    st = desk.bots["stacker"]
    with pytest.raises(ValueError, match="no XRP in the hold stack"):
        st.move_out("XRP-USD")
    with pytest.raises(ValueError, match="don't hold any XRP"):
        st.move_in("XRP-USD")
    stack(desk)
    desk.bus.publish("signal.proposed", Signal("SOL-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    with pytest.raises(ValueError, match="already have a SOL trade open"):
        st.move_out("SOL-USD")


def test_moves_are_not_learned_from_as_trades():
    desk = make()
    stack(desk)
    st = desk.bots["stacker"]
    st.move_out("SOL-USD")
    run(desk.bus.drain())
    st.move_in("SOL-USD")
    run(desk.bus.drain())
    assert desk.bots["reviewer"].stats == {} and desk.bots["targets"].runs == {}
    assert desk.bots["coach"].learned_trades == 0


def test_on_real_money_the_records_and_cash_still_match_kraken():
    fk = FakeKraken(usd=2_000.0, held={"XXDG": 300.0})
    s = Settings(feed="coinbase", live_venue="kraken", kraken_key="KEY", kraken_secret=SECRET,
                 symbols=["BTC-USD", "DOGE-USD"])
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker(fk))
    run(desk.live_broker.refresh_market())
    desk.owner_protected = lambda: True
    run(desk.switch_mode("live"))
    for sym, px in (("BTC-USD", 60_000.0), ("DOGE-USD", 0.2)):
        prime(desk, sym, px)
    st, pm = desk.bots["stacker"], desk.bots["portfolio"]
    desk.live_broker.baseline = {}  # (say the 300 DOGE were bought into the hold stack, not owned before)
    st.books["live"]["DOGE-USD"] = {"qty": 300.0, "cost": 55.0, "buys": 4}
    pm.outside["DOGEUSD"] = 300.0
    st.move_out("DOGE-USD", 100.0)
    run(desk.bus.drain())
    assert desk.portfolio.position("DOGE-USD").qty == pytest.approx(100.0) and pm.outside["DOGEUSD"] == pytest.approx(200.0)
    pm.last_fill_ts = 0
    for _ in range(3):
        assert run(pm.reconcile_positions()) == []  # 100 the bots trade + 200 in the stack = Kraken's 300
        pm.sync_cash(run(desk.live_broker.account()))
    assert desk.portfolio.cash == pytest.approx(fk.bal["ZUSD"])
    st.move_in("DOGE-USD")
    run(desk.bus.drain())
    for _ in range(3):
        assert run(pm.reconcile_positions()) == []
    assert pm.outside["DOGEUSD"] == pytest.approx(300.0) and desk.portfolio.cash == pytest.approx(fk.bal["ZUSD"])
    assert "AddOrder" not in fk.calls  # nothing was traded on Kraken: the coins never moved


def test_move_from_the_app():
    from fastapi.testclient import TestClient

    from tradezone.server import create_app
    desk = make()
    stack(desk)
    with TestClient(create_app(desk, run_desk=False)) as c:
        r = c.post("/api/stack/move", json={"symbol": "SOL-USD", "direction": "out", "fraction": 0.5})
        assert r.status_code == 200 and r.json()["moved"] == pytest.approx(1.0)
        assert c.post("/api/stack/move", json={"symbol": "SOL-USD", "direction": "in"}).status_code == 200
        assert c.post("/api/stack/move", json={"symbol": "XRP-USD", "direction": "out"}).status_code == 400
        assert c.post("/api/stack/move", json={"symbol": "SOL-USD", "direction": "up"}).status_code == 400
    assert desk.bots["stacker"].book["SOL-USD"]["qty"] == pytest.approx(2.0)
