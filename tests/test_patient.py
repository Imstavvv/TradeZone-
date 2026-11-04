"""Patient exits (real money): a wider safety stop; a losing trade is held for a comeback and cut early only
when momentum AND volume say it isn't coming back."""

import pytest

from tests.helpers import capture, prime, run
from tests.test_quick_flips import buy, make, tick
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
    bar(desk, "BTC-USD", 50_700.0, mom=0.2)  # back above entry + fees
    assert pos.is_flat and trades[-1].pnl > 0
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
