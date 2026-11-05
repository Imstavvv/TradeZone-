"""Patient exits (real money): a wider safety stop; a losing trade is held for a comeback and cut early only
when momentum AND volume say it isn't coming back."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_quick_flips import buy, tick
from tests.test_quick_flips import make as _make


def make(**kw):
    kw.setdefault("patient_exits", True)  # (off by default now; these tests cover the feature itself)
    return _make(**kw)
from tradezone.core.models import Snapshot, Tick


def bar(desk, sym, price, mom=0.0, vr=1.0, swing_low=None):
    vol = 0.002
    ind = {"atr": price * 0.004, "volatility": vol, "rsi": 45, "ema_fast": price * (1 + 0.8 * mom * vol),
           "ema_slow": price, "sma50": price, "donchian_high": price * 1.05, "donchian_low": price * 0.9,
           "swing_low": swing_low if swing_low is not None else price * 0.98, "swing_high": price * 1.05,
           "volume_ratio": vr}
    tick(desk, sym, price)
    desk.bus.publish("market.snapshot", Snapshot(sym, price, ind))
    run(desk.bus.drain())


def test_real_money_trades_get_a_wider_safety_stop_and_more_capital():
    desk = make()
    pos = buy(desk)
    assert pos.stop_price == pytest.approx(50_000 * 0.95, rel=1e-3)  # -5% safety stop (bigger coins)
    assert desk.bots["portfolio"].patient == {"BTC-USD"}
    assert desk.risk_info()["risk_per_trade"] == pytest.approx(0.025)


def test_a_dip_without_selling_pressure_is_held_for_a_comeback():
    desk = make()
    pos = buy(desk)
    bar(desk, "BTC-USD", 48_500.0, mom=-0.3, vr=1.0)  # -3%: past the usual -2% stop, but no real pressure
    assert pos.qty > 0 and "BTC-USD" in desk.bots["portfolio"].watch


def test_momentum_and_heavy_volume_against_it_cut_the_loss_early():
    desk = make()
    pos = buy(desk)
    trades = capture(desk, "trade.closed")
    bar(desk, "BTC-USD", 48_700.0, mom=-0.9, vr=2.5)  # strong selling on 2.5x the usual volume
    assert pos.is_flat and trades[-1].pnl < 0
    assert desk.bots["portfolio"].metrics["patient_cuts"] == 1


def test_strong_momentum_alone_isnt_enough_volume_must_confirm():
    desk = make()
    pos = buy(desk)
    bar(desk, "BTC-USD", 48_700.0, mom=-0.9, vr=0.8)  # falling, but on light volume: may well come back
    assert pos.qty > 0


def test_a_trade_that_comes_back_is_closed_in_profit():
    desk = make()
    pos = buy(desk)
    bar(desk, "BTC-USD", 48_600.0, mom=-0.3)  # down: held
    trades = capture(desk, "trade.closed")
    bar(desk, "BTC-USD", 50_700.0, mom=0.2)  # back above entry + fees, but the profit doesn't beat the fees yet
    assert not pos.is_flat
    bar(desk, "BTC-USD", 51_000.0, mom=0.2)  # now it keeps more than the fees it paid
    assert trades and all(t.pnl > 0 for t in trades)
    sold = sum(t.qty for t in trades)
    fees = sold * 50_500 * 2 * 0.0040  # both fees on what was sold (~0.40% a side)
    assert sum(t.pnl for t in trades) >= fees  # kept more than the fees
    assert desk.bots["portfolio"].metrics["patient_rescues"] == 1


def test_without_volume_data_a_break_of_the_recent_low_stands_in():
    desk = make()
    pos = buy(desk)
    bar(desk, "BTC-USD", 48_700.0, mom=-0.9, vr=None, swing_low=49_000.0)  # below the recent swing low
    assert pos.is_flat


def test_the_safety_stop_still_always_works():
    desk = make()
    pos = buy(desk)
    tick(desk, "BTC-USD", 47_400.0)  # through the -5% stop
    assert pos.is_flat


def test_practice_keeps_the_usual_tight_stop():
    desk = make(quick=False)
    desk.settings.patient_exits = True
    run(desk.switch_mode("paper"))
    prime(desk, "BTC-USD", 50_000.0, atr=250.0)
    from tradezone.core.models import Side, Signal
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    p = desk.portfolio.position("BTC-USD")
    assert p.stop_price == pytest.approx(50_000 * 0.97, rel=1e-3) and not desk.bots["portfolio"].patient


def test_scout_measures_volume_against_the_recent_average():
    desk = make()
    scout = desk.bots["scout"]
    snaps = capture(desk, "market.snapshot")
    for i in range(60):
        desk.bus.publish("market.tick", Tick("ETH-USD", 3_000.0 + i, 10.0 if i < 59 else 30.0, 1_000_000 + i * 60))
        run(desk.bus.drain())
    desk.bus.publish("market.tick", Tick("ETH-USD", 3_100.0, 1.0, 1_000_000 + 60 * 60))
    run(desk.bus.drain())
    assert scout.bar_seconds == 60 and snaps[-1].indicators["volume_ratio"] == pytest.approx(3.0, rel=0.01)


def test_every_profit_sale_keeps_more_than_its_fees():
    """A profit-taking sale that wouldn't beat the trade's fees is turned down (stops and cuts never are)."""
    from tradezone.core.models import Side, Signal, SignalKind
    desk = make()
    pos = buy(desk)
    rejected = capture(desk, "signal.rejected")
    desk.prices["BTC-USD"] = 50_500.0
    pos.last_price = 50_500.0
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.SELL, 1.0, "bank it", "skimmer", SignalKind.EXIT,
                                               fraction=0.5, calm=True))
    run(desk.bus.drain())
    assert pos.qty == 0.035 and "doesn't beat" in rejected[-1]["reason"]
    pm = desk.bots["portfolio"]
    assert "BTC-USD" not in pm.exiting  # nothing left stuck 'on its way out'
    pos.stop_price = 50_400.0  # a stop is never held back by the rule
    desk.bus.publish("market.tick", __import__("tradezone.core.models", fromlist=["Tick"]).Tick("BTC-USD", 50_390.0))
    run(desk.bus.drain())
    assert pos.is_flat


def test_the_locked_profit_stop_keeps_more_than_the_fees_when_turned_on():
    desk = make()
    desk.settings.fee_rule_stops = True  # (opt-in: TZ_FEE_RULE_STOPS=true)
    pos = buy(desk)
    s = desk.settings
    pos.take_profit = None  # (let it run past +1R, where the lock starts)
    for px in (50_500.0, 51_500.0, 53_000.0):  # a winner: the stop is raised to lock profit in
        bar(desk, "BTC-USD", px, mom=0.5)
    assert pos.stop_price > pos.avg_price
    assert s.net_if_sold(pos, pos.stop_price) >= s.fees_if_sold(pos, pos.stop_price)  # stopped there, it still beats the fees


def test_by_default_a_locked_stop_still_covers_break_even_and_the_fees():
    desk = make()
    pos = buy(desk)
    pos.take_profit = None
    for px in (50_500.0, 51_500.0, 53_000.0):
        bar(desk, "BTC-USD", px, mom=0.5)
    assert desk.settings.net_if_sold(pos, pos.stop_price) > 0  # a stop in profit never turns into a loss after fees


def test_a_trade_back_in_profit_is_never_cut_while_it_waits_to_beat_its_fees():
    desk = make()
    pos = buy(desk)
    bar(desk, "BTC-USD", 48_600.0, mom=-0.3)  # down: on watch
    bar(desk, "BTC-USD", 50_700.0, mom=-0.9, vr=3.0)  # back in profit (less than the fees), momentum dips hard
    assert pos.qty > 0 and desk.bots["portfolio"].metrics.get("patient_cuts", 0) == 0
