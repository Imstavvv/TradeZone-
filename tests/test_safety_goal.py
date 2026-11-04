"""'Don't lose my money': stricter real-money limits and a capital floor; the $50 -> $10,000 goal."""

import math

import pytest

from tests.helpers import run
from tests.test_live_switch import make_desk


def live_desk():
    desk = make_desk()
    run(desk.switch_mode("live"))
    return desk


def test_real_money_stops_at_minus_5_percent_a_day():
    desk = live_desk()
    p = desk.portfolio
    p.cash -= p.equity * 0.051  # a -5.1% day
    run(desk.bots["risk"].on_cycle())
    assert desk.halted and desk.halt_kind == "daily"


def test_capital_floor_stops_everything_until_the_owner_restarts():
    desk = live_desk()
    p = desk.portfolio
    p.day_start_equity = p.peak_equity = p.equity * 0.84  # not a daily or drawdown loss...
    p.cash -= p.equity * 0.16  # ...but 16% below what was put in
    run(desk.bots["risk"].on_cycle())
    assert desk.halted and desk.halt_kind == "floor" and "below what you put in" in desk.halt_reason
    desk.reset_halt()  # the owner decides to carry on
    run(desk.bots["risk"].on_cycle())
    assert not desk.halted  # the floor re-based from here, so it doesn't instantly trip again


def test_practice_mode_keeps_the_normal_limits(desk):
    p = desk.portfolio
    p.cash -= p.equity * 0.031
    run(desk.bots["risk"].on_cycle())
    assert not desk.halted


def test_goal_tracks_growth_on_a_50_dollar_scale(desk):
    g = desk.state()["goal"]
    assert (g["start"], g["target"], g["value"]) == (50.0, 10_000.0, 50.0) and g["progress"] == 0
    desk.portfolio.cash *= 2  # the account doubled
    g = desk.goal_status()
    assert g["value"] == pytest.approx(100.0) and g["growth"] == pytest.approx(2.0)
    assert g["progress"] == pytest.approx(math.log(2) / math.log(200), abs=1e-3)  # one "doubling" of ~7.6
    assert g["next_milestone"] == 250.0 and g["days_at_1pct"] > 400  # honest: even 1%/day takes over a year
    desk.portfolio.cash *= 100
    assert desk.goal_status()["reached"]


def test_goal_can_be_changed_and_is_validated(desk):
    assert desk.set_goal(100, 1000)["target"] == 1000
    with pytest.raises(ValueError):
        desk.set_goal(100, 50)
