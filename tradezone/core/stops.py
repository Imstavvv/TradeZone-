"""Stop-loss and target placement using the most widely used method.

Long positions put the stop **just below the most recent swing low**. That's the last dip buyers
defended, and it's where most traders put their stops. A volatility buffer (a fraction of ATR)
keeps ordinary noise from tagging it. The stop is also nudged off round numbers (100, 105,
65,000...), because stops cluster there and get "hunted". Shorts mirror this above the swing high.

Guard rails:

* the stop is never closer than ``min_stop_atr`` x ATR (inside the noise)
* a trade whose structural stop is wider than ``max_stop_atr`` x ATR is skipped (risk too wide)
* the target is ``reward_risk`` x the stop distance (2:1 by default)
* there must be at least ``min_room_r`` x the stop distance before the nearest resistance (support
  for shorts), otherwise the trade has no room to work
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class StopPlan:
    stop: float
    target: float
    distance: float  # |entry - stop|, i.e. 1R
    method: str  # human-readable description
    atr_multiple: float


class StopRejected(ValueError):
    pass


def round_step(price: float) -> float:
    """Spacing of 'round numbers' traders watch: 2 significant digits, halved (e.g. 100 -> 5)."""
    if price <= 0:
        return 0.0
    return 10 ** (math.floor(math.log10(price)) - 1) / 2


def avoid_round_number(stop: float, direction: int, atr: float, price: float) -> tuple[float, bool]:
    """If the stop sits on or just inside a round number, move it a little beyond that number."""
    step = round_step(price)
    if step <= 0:
        return stop, False
    near = round(stop / step) * step
    margin = 0.25 * atr
    if abs(stop - near) <= margin:
        # longs: below the level, shorts: above it
        return near - direction * margin, True
    return stop, False


def plan_stop(
    direction: int,
    price: float,
    atr: float,
    swing_low: float | None,
    swing_high: float | None,
    resistance: float | None,
    support: float | None,
    *,
    buffer_atr: float = 1.0,
    min_stop_atr: float = 1.5,
    max_stop_atr: float = 4.0,
    reward_risk: float = 2.0,
    min_room_r: float = 1.0,
) -> StopPlan:
    """Plan stop and target for a new position. ``direction`` is +1 (long) or -1 (short)."""
    if atr <= 0 or price <= 0:
        raise StopRejected("no volatility data")
    level = swing_low if direction > 0 else swing_high
    notes = []
    if level is None or (level - price) * direction >= 0:
        # no usable swing point on the right side of price: fall back to a volatility stop
        stop = price - direction * 2.0 * atr
        notes.append("2x ATR (no swing level)")
    else:
        stop = level - direction * buffer_atr * atr
        notes.append(f"{'below swing low' if direction > 0 else 'above swing high'} {level:,.6g}"
                     f" - {buffer_atr:g} ATR buffer")
    stop, moved = avoid_round_number(stop, direction, atr, price)
    if moved:
        notes.append("moved off round number")
    dist = (price - stop) * direction
    if dist < min_stop_atr * atr:
        stop = price - direction * min_stop_atr * atr
        dist = min_stop_atr * atr
        notes.append(f"widened to {min_stop_atr:g} ATR minimum")
    if dist > max_stop_atr * atr:
        raise StopRejected(
            f"nearest swing stop is {dist / atr:.1f} ATR away (max {max_stop_atr:g}) - risk too wide"
        )
    barrier = resistance if direction > 0 else support
    if barrier is not None and (barrier - price) * direction > 0:
        room = (barrier - price) * direction
        if room < min_room_r * dist:
            raise StopRejected(
                f"only {room / dist:.1f}R of room to {'resistance' if direction > 0 else 'support'} "
                f"at {barrier:,.6g} (need {min_room_r:g}R)"
            )
    target = price + direction * reward_risk * dist
    return StopPlan(stop, target, dist, "; ".join(notes), dist / atr)
