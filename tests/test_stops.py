import pytest

from tradezone.core.models import Side, Signal, Snapshot
from tradezone.core.stops import StopRejected, avoid_round_number, plan_stop, round_step
from tests.test_bots import capture, prime, run


def test_round_steps():
    assert round_step(100) == 5 and round_step(65_000) == 500 and round_step(2.5) == 0.05


def test_long_stop_below_swing_low_with_buffer():
    p = plan_stop(1, price=103.2, atr=0.4, swing_low=102.2, swing_high=None, resistance=None, support=None)
    assert p.stop == pytest.approx(102.2 - 0.4)  # 1 ATR below the swing low
    assert p.target == pytest.approx(103.2 + 2 * p.distance)  # 2:1 reward:risk
    assert "below swing low" in p.method


def test_short_stop_above_swing_high():
    p = plan_stop(-1, price=96.8, atr=0.4, swing_low=None, swing_high=97.8, resistance=None, support=None)
    assert p.stop == pytest.approx(98.2) and p.target < 96.8


def test_stop_moved_off_round_number():
    stop, moved = avoid_round_number(100.05, 1, atr=0.4, price=101)
    assert moved and stop == pytest.approx(99.9)  # just below 100, not on it
    p = plan_stop(1, price=101.9, atr=0.6, swing_low=100.65, swing_high=None, resistance=None, support=None)
    assert p.stop < 100 and "round number" in p.method


def test_too_tight_is_widened_and_too_wide_is_rejected():
    p = plan_stop(1, price=103.2, atr=0.4, swing_low=103.1, swing_high=None, resistance=None, support=None)
    assert p.distance == pytest.approx(1.5 * 0.4)
    with pytest.raises(StopRejected, match="too wide"):
        plan_stop(1, price=103.2, atr=0.4, swing_low=99.0, swing_high=None, resistance=None, support=None)


def test_needs_room_to_resistance():
    with pytest.raises(StopRejected, match="room to resistance"):
        plan_stop(1, price=103.2, atr=0.4, swing_low=102.7, swing_high=None, resistance=103.4, support=None)
    plan_stop(1, price=103.2, atr=0.4, swing_low=102.7, swing_high=None, resistance=None, support=None)


def test_no_swing_level_falls_back_to_atr():
    p = plan_stop(1, price=103.2, atr=0.4, swing_low=104.0, swing_high=None, resistance=None, support=None)
    assert p.distance == pytest.approx(0.8) and "ATR" in p.method


def prime_structure(desk, swing_low=99.2):
    prime(desk, price=100.0, atr=0.4)
    s = desk.bots["risk"].snapshots["AAPL"]
    s.indicators.update(swing_low=swing_low, swing_high=101.0, donchian_high=99.9, donchian_low=97.0)


def test_risk_manager_uses_structural_stop(desk):
    prime_structure(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("AAPL")
    assert pos.stop_price == pytest.approx(98.8)  # swing low 99.2 - 1 ATR
    assert pos.take_profit == pytest.approx(100 + 2 * 1.2, rel=1e-3)
    # sized so that hitting the stop loses ~1% of equity at most
    assert pos.qty * (pos.avg_price - pos.stop_price) <= desk.portfolio.equity * 0.0101


def test_wide_structure_skips_trade(desk):
    prime_structure(desk, swing_low=97.0)  # 3 points below = 7.5 ATR + buffer
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert "too wide" in rejected[0]["reason"] and desk.portfolio.position("AAPL").is_flat


def test_portfolio_heat_cap(desk):
    desk.settings.risk.max_portfolio_heat = 0.004  # room for about one trade (~0.3% each)
    for sym in ("AAPL", "NVDA"):
        prime(desk, sym, 100.0)
    rejected = capture(desk, "signal.rejected")
    for sym in ("AAPL", "NVDA"):
        desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 1.0, "t", "quant"))
        run(desk.bus.drain())
    risk = desk.bots["risk"]
    assert risk.open_risk() <= desk.portfolio.equity * 0.004 + 1
    assert desk.portfolio.position("NVDA").is_flat or desk.portfolio.position("NVDA").qty < desk.portfolio.position("AAPL").qty
