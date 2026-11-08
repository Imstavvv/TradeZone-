"""Buying in a falling market (asked for: "don't skip falling markets - those could be good buy areas")."""

from tests.helpers import capture, prime, run
from tests.test_new_bots import advice, make, snap
from tradezone.bots.advisors import buy_area, falling_buy_ok
from tradezone.core.models import Side, Signal


def falling(desk, sym_px=None, **kw):
    """Most of the market down on the hourly chart."""
    for s in ("BTC-USD", "SOL-USD", "DOGE-USD", "XRP-USD"):
        snap(desk, s, 100.0, tf_1h_trend=-1)
    return snap(desk, "ETH-USD", 100.0, tf_1h_trend=-1, **kw)


def test_buy_areas():
    class S:
        def __init__(self, price, **ind):
            self.price, self.indicators = price, ind
    assert "oversold" in buy_area(S(100, rsi=30))
    assert "hourly" in buy_area(S(100, rsi=50, tf_1h_rsi=33))
    assert "bottom fifth" in buy_area(S(100, rsi=50, tf_1d_range_pos=0.1, tf_1d_candles=10))
    assert "support" in buy_area(S(100.5, rsi=50, swing_low=100.0))
    assert buy_area(S(110, rsi=50, swing_low=100.0, donchian_low=95.0)) is None


def test_the_modes():
    class Set:
        falling_buys = "block"
    class Snap:
        price, indicators = 100.0, {"rsi": 30}
    s = Set()
    assert falling_buy_ok(s, Snap())[0] is False
    s.falling_buys = "areas"
    assert falling_buy_ok(s, Snap())[0] is True
    Snap.indicators = {"rsi": 55}
    assert falling_buy_ok(s, Snap())[0] is False
    s.falling_buys = "all"
    assert falling_buy_ok(s, Snap())[0] is True


def _buy(desk, rsi):
    prime(desk, "ETH-USD", 100.0, atr=0.5)
    falling(desk, rsi=rsi, atr=0.5)
    rej = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.BUY, 0.95, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position("ETH-USD").qty, [r["reason"] for r in rej]


def test_a_falling_market_at_a_buy_area_is_bought_smaller():
    desk = make(falling_buys="block")
    qty, why = _buy(desk, 30)
    assert qty == 0 and any("most of the market is falling" in w for w in why)
    desk = make(falling_buys="areas")
    qty_area, _ = _buy(desk, 30)
    assert qty_area > 0 and desk.bots["risk"].metrics["falling_buys_allowed"] == 1
    desk = make(falling_buys="areas")
    qty, why = _buy(desk, 55)  # (falling and not at a buy area: still waits)
    assert qty == 0


def test_bitcoin_falling_no_longer_vetoes_a_buy_area():
    desk = make(falling_buys="areas")
    w = desk.bots["weather"]
    w.btc.extend([100.0] + [95.0] * w.BARS)
    w.change = -0.05
    snap(desk, "ETH-USD", 100.0, rsi=30)
    assert advice(desk, "weather") == (None, -0.03, 0.7)
    snap(desk, "ETH-USD", 100.0, rsi=55)
    assert "Bitcoin is falling" in advice(desk, "weather")[0]
