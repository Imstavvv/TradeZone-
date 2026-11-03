"""Small shared helpers used by several bots (one implementation each)."""

from __future__ import annotations

import math
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from .models import Side

_NY = ZoneInfo("America/New_York")


def us_market_open(at: datetime | None = None) -> bool:
    """Regular US equity session (Mon-Fri 09:30-16:00 New York). Exchange holidays aren't modelled."""
    t = (at or datetime.now(_NY)).astimezone(_NY)
    return t.weekday() < 5 and dtime(9, 30) <= t.time() < dtime(16, 0)


def round_qty(qty: float, price: float, fractional: bool = True) -> float:
    """Round an order size *down*, to what the venue accepts (fractional crypto/stocks, or whole shares)."""
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    if fractional:
        f = 10 ** (6 if price > 1000 else 4)
        return math.floor(qty * f) / f
    return float(math.floor(qty))


def norm_symbol(symbol: str) -> str:
    """Compare symbols across venues: BTC-USD, BTC/USD and BTCUSD are the same market."""
    return symbol.replace("-", "").replace("/", "").upper()


def is_closing(position_qty: float, side: Side) -> bool:
    """True when an order on ``side`` would reduce the existing position."""
    return (position_qty > 0 and side is Side.SELL) or (position_qty < 0 and side is Side.BUY)


def position_mismatches(ours: dict[str, float], theirs: dict[str, float],
                        rel_tol: float = 1e-6) -> dict[str, tuple[float, float]]:
    """Symbols whose quantities differ between our book and the broker: {norm_symbol: (ours, theirs)}."""
    a = {norm_symbol(k): v for k, v in ours.items() if abs(v) > 1e-12}
    b = {norm_symbol(k): v for k, v in theirs.items() if abs(v) > 1e-12}
    out = {}
    for sym in set(a) | set(b):
        x, y = a.get(sym, 0.0), b.get(sym, 0.0)
        if abs(x - y) > rel_tol * max(1.0, abs(y)):
            out[sym] = (x, y)
    return out
