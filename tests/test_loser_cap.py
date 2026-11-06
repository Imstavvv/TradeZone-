"""The underwater cap: while trades not yet back above break-even tie up 15% of the balance, no new buys.

Tested on real price history in rising, falling and sideways markets: with never-sell-at-a-loss on, a slide
otherwise swallows the whole balance into held losers (about half the loss when prices fall, less profit
when they only rise - so it can be switched off)."""

from fastapi.testclient import TestClient

from tests.helpers import capture, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, SignalKind
from tradezone.core.portfolio import Position
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app

SYMS = ["BTC-USD", "ETH-USD", "SOL-USD"]


def make(db=":memory:", **kw):
    s = Settings(feed="sim", symbols=list(SYMS), starting_cash=1_000.0, **kw)
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    d.prices.update({"BTC-USD": 60_000.0, "ETH-USD": 3_000.0, "SOL-USD": 150.0})
    return d


def hold(desk, sym, cost, now_px, avg):
    qty = cost / avg
    desk.portfolio.positions[sym] = Position(symbol=sym, qty=qty, avg_price=avg, last_price=now_px)
    desk.portfolio.cash -= cost
    desk.prices[sym] = now_px


def idea(desk, sym, kind=SignalKind.ENTRY, side=Side.BUY):
    out = capture(desk, "signal.rejected")
    desk.bots["risk"].evaluate(Signal(sym, side, 0.99, "test", "quant", kind=kind))
    run(desk.bus.drain())
    return out[-1]["reason"] if out else None


def test_it_is_on_by_default_at_the_tested_15_percent():
    d = make()
    assert d.settings.loser_cap == 0.15 and d.settings.loser_cap_depth == 0.005
    assert d.BEST["loser_cap"] == 0.15 and d.protocol_status()["off"] == []


def test_no_new_buys_while_losers_tie_up_the_cap_and_buying_resumes_when_they_recover():
    d = make()
    hold(d, "BTC-USD", cost=200.0, now_px=54_000.0, avg=60_000.0)  # 20% of the balance, 10% under
    assert "underwater cap" in (idea(d, "SOL-USD") or "")
    st = d.loser_cap_status()
    assert st["blocking"] and 0.15 <= st["share"] <= 0.25
    d.prices["BTC-USD"] = d.portfolio.positions["BTC-USD"].last_price = 63_000.0  # back above break-even
    assert not d.loser_cap_status()["blocking"]
    assert "underwater cap" not in (idea(d, "SOL-USD") or "")


def test_a_small_loser_under_the_cap_doesnt_block():
    d = make()
    hold(d, "BTC-USD", cost=100.0, now_px=54_000.0, avg=60_000.0)  # 10% of the balance
    assert "underwater cap" not in (idea(d, "SOL-USD") or "")


def test_it_never_blocks_selling():
    d = make()
    hold(d, "BTC-USD", cost=300.0, now_px=54_000.0, avg=60_000.0)  # the cap is hit
    hold(d, "ETH-USD", cost=100.0, now_px=3_600.0, avg=3_000.0)  # a winner
    assert "underwater cap" not in (idea(d, "ETH-USD", kind=SignalKind.EXIT, side=Side.SELL) or "")


def test_switched_off_buys_go_ahead():
    d = make(loser_cap=0.0)
    hold(d, "BTC-USD", cost=400.0, now_px=54_000.0, avg=60_000.0)
    assert "underwater cap" not in (idea(d, "SOL-USD") or "")


def test_the_switch_is_remembered_and_counts_as_off_the_tested_best(tmp_path):
    db = str(tmp_path / "tz.db")
    d = make(db)
    with TestClient(create_app(d, run_desk=False)) as c:
        assert c.post("/api/loser-cap", json={"on": False}).json()["cap"] == 0.0
        assert "loser_cap" in {x["key"] for x in d.protocol_status()["off"]}
        assert c.get("/api/state").json()["loser_cap"]["cap"] == 0.0
    d2 = make(db)
    run(d2.start())
    assert d2.settings.loser_cap == 0.0
    d2.apply_best()
    assert d2.settings.loser_cap == 0.15
    run(d2.shutdown())
    d3 = make(db)
    run(d3.start())
    assert d3.settings.loser_cap == 0.15
    run(d3.shutdown())
