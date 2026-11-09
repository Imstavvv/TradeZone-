
"""Never sell at a loss: a trade below break-even after fees is held until it recovers."""

import pytest

from tests.helpers import capture, run
from tests.test_patient import bar
from tests.test_quick_flips import buy, tick
from tests.test_quick_flips import make as _make
from tradezone.core.models import Side, Signal, SignalKind


def make(**kw):
    kw.setdefault("patient_exits", True)
    kw.setdefault("max_loss_pct", 0.0)  # never-sell on its own (the loss floor has its own tests)
    return _make(**kw)


def test_on_by_default():
    assert make().settings.never_sell_at_loss is True


def test_a_stop_below_break_even_doesnt_sell_the_trade_is_held():
    desk = make()
    pos = buy(desk)
    tick(desk, "BTC-USD", pos.avg_price * 0.60)  # -40%: through the -33% stop
    assert pos.qty > 0 and desk.bots["portfolio"].metrics.get("held_not_sold", 0) >= 1


def test_momentum_and_volume_turning_against_it_dont_cut_it_either():
    desk = make()
    pos = buy(desk)
    bar(desk, "BTC-USD", 46_000.0, mom=-0.9, vr=3.0)
    assert pos.qty > 0 and desk.bots["portfolio"].metrics.get("patient_cuts", 0) == 0


def test_once_it_recovers_it_is_sold_at_a_profit():
    desk = make()
    pos = buy(desk)
    tick(desk, "BTC-USD", pos.avg_price * 0.80)
    trades = capture(desk, "trade.closed")
    tick(desk, "BTC-USD", pos.take_profit * 1.001)  # back up through its target
    assert trades and all(t.pnl > 0 for t in trades)


def test_the_daily_loss_limit_stops_new_trades_but_doesnt_sell():
    desk = make()
    pos = buy(desk)
    tick(desk, "BTC-USD", pos.avg_price * 0.70)
    desk.portfolio.cash -= 0.06 * desk.portfolio.equity  # a bad day
    run(desk.bots["risk"].on_cycle())
    run(desk.bus.drain())
    assert desk.halted and pos.qty > 0  # trading stopped, the trade is held


def test_your_own_close_all_and_emergency_stop_still_sell():
    for action in ("flatten", "kill"):
        desk = make()
        pos = buy(desk)
        tick(desk, "BTC-USD", pos.avg_price * 0.80)
        if action == "flatten":
            desk.flatten("operator flatten")
        else:
            desk.halt("manual kill", flatten=True)
        run(desk.bus.drain())
        assert pos.is_flat  # you asked: sold even at a loss


def test_no_bot_can_sell_at_a_loss_through_the_risk_manager():
    desk = make()
    pos = buy(desk)
    tick(desk, "BTC-USD", pos.avg_price * 0.90)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 1.0, "some bot", "skimmer", SignalKind.EXIT))
    run(desk.bus.drain())
    assert pos.qty > 0 and "never sell at a loss" in rejected[-1]["reason"]


def test_meme_coins_trade_at_half_the_usual_biggest_size():
    desk, old = make(), make(never_sell_at_loss=False)
    for d in (desk, old):
        d.settings.risk.risk_per_trade = d.settings.live_risk_per_trade = 0.5  # so the size cap is what binds
    meme, normal = buy(desk, "PEPE-USD", 0.00001), buy(old, "PEPE-USD", 0.00001)
    assert meme.qty * 0.00001 <= 0.51 * normal.qty * 0.00001


def test_can_be_switched_off():
    desk = make(never_sell_at_loss=False)
    pos = buy(desk)
    tick(desk, "BTC-USD", pos.avg_price * 0.60)
    assert pos.is_flat  # the -33% stop sells as before


def test_switch_from_the_dashboard_and_it_is_remembered(tmp_path):
    from fastapi.testclient import TestClient

    from tradezone.config import Settings
    from tradezone.desk import TradingDesk
    from tradezone.journal import Journal
    from tradezone.market.simulated import SimulatedFeed
    from tradezone.server import create_app
    db = str(tmp_path / "tz.db")
    s = Settings(feed="sim", symbols=["BTC-USD"])
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    with TestClient(create_app(desk, run_desk=False)) as c:
        assert c.post("/api/never-sell", json={"on": False}).json() == {"never_sell_at_loss": False}
        assert c.get("/api/state").json()["never_sell_at_loss"] is False
    s2 = Settings(feed="sim", symbols=["BTC-USD"])
    d2 = TradingDesk(s2, feed=SimulatedFeed(s2.symbols), journal=Journal(db))
    run(d2.start())
    assert d2.settings.never_sell_at_loss is False
    run(d2.shutdown())


def test_the_stop_message_says_trades_are_held_not_sold():
    desk = make()
    pos = buy(desk)
    tick(desk, "BTC-USD", pos.avg_price * 0.70)
    desk.portfolio.cash -= 0.06 * desk.portfolio.equity
    run(desk.bots["risk"].on_cycle())
    assert "held, never sold at a loss" in desk.halt_reason


# ---- the loss floor: never sell at a loss - except a trade 20% under what was paid --------------------
def test_the_loss_floor_sells_a_trade_20_percent_down_even_with_never_sell_on():
    desk = make(max_loss_pct=0.20)
    pos = buy(desk)
    trades = capture(desk, "trade.closed")
    tick(desk, "BTC-USD", pos.avg_price * 0.82)  # -18%: still held
    assert pos.qty > 0 and not trades
    tick(desk, "BTC-USD", pos.avg_price * 0.79)  # -21%: past the floor and through the -20% stop
    assert pos.is_flat and trades and trades[-1].pnl < 0
    assert trades[-1].pnl > -0.25 * trades[-1].qty * trades[-1].entry_price  # about a fifth lost, not more


@pytest.mark.entry_filters  # (the real defaults: the preset includes the entry filters)
def test_the_floor_is_on_by_default_and_part_of_the_tested_best_settings():
    from tradezone.config import Settings
    from tradezone.desk import TradingDesk
    from tradezone.journal import Journal
    d = TradingDesk(Settings(feed="sim", symbols=["BTC-USD"]), journal=Journal(":memory:"))
    assert d.settings.max_loss_pct == 0.07 and d.settings.wide_stop_pct == 0.07  # at most -7% (asked for)
    assert d.BEST["max_loss_pct"] == 0.07 and d.protocol_status()["off"] == []
    assert d.set_loss_floor(False)["max_loss_pct"] == 0.0 and "max_loss_pct" in {x["key"] for x in d.protocol_status()["off"]}
