"""Volume snipes: any crypto or meme coin trading several times its usual volume while the price jumps up."""

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Snapshot
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(**kw):
    s = Settings(feed="sim", symbols=["BTC-USD", "SOL-USD", "DOGE-USD", "AAPL"], **kw)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def bar(desk, sym, price, vr=1.0, rsi=60.0, ema_fast=None, ema_slow=None):
    ind = {"atr": price * 0.004, "volatility": 0.002, "rsi": rsi, "ema_fast": ema_fast or price * 0.998,
           "ema_slow": ema_slow or price * 0.997, "sma50": price, "donchian_high": price * 1.01,
           "donchian_low": price * 0.95, "swing_high": price * 1.05, "swing_low": price * 0.97, "volume_ratio": vr}
    desk.bus.publish("market.snapshot", Snapshot(sym, price, ind))
    run(desk.bus.drain())


def ideas(desk):
    return capture(desk, "signal.proposed")


def test_snipes_a_coin_whose_volume_surges_while_the_price_jumps():
    desk = make()
    got = ideas(desk)
    prime(desk, "SOL-USD", 150.0)
    bar(desk, "SOL-USD", 150.0)
    bar(desk, "SOL-USD", 151.5, vr=4.0)  # +1% on 4x the usual volume
    snipes = [i for i in got if "volume snipe" in i.reason]
    assert snipes and snipes[0].symbol == "SOL-USD" and snipes[0].side.value == "buy"
    assert desk.bots["quant"].metrics["volume_snipes"] == 1


def test_meme_coins_too_and_only_once_per_surge():
    desk = make()
    got = ideas(desk)
    for px, vr in ((0.20, 1.0), (0.205, 5.0), (0.21, 5.0), (0.215, 5.0)):
        bar(desk, "DOGE-USD", px, vr=vr)
    assert sum("volume snipe" in i.reason for i in got) == 1


def test_no_snipe_without_the_volume_or_the_price_move_or_when_overheated():
    for sym, steps in (("SOL-USD", ((150.0, 1.0), (151.5, 1.5))),  # a jump on ordinary volume
                       ("BTC-USD", ((60_000.0, 1.0), (60_010.0, 5.0)))):  # huge volume, price flat
        desk = make()
        got = ideas(desk)
        for px, vr in steps:
            bar(desk, sym, px, vr=vr)
        assert not any("volume snipe" in i.reason for i in got)
    desk = make()
    got = ideas(desk)
    bar(desk, "SOL-USD", 150.0)
    bar(desk, "SOL-USD", 153.0, vr=5.0, rsi=88.0)  # already wildly overbought
    assert not any("volume snipe" in i.reason for i in got)


def test_not_into_a_falling_trend_and_never_stocks():
    desk = make()
    got = ideas(desk)
    bar(desk, "SOL-USD", 150.0)
    bar(desk, "SOL-USD", 151.0, vr=5.0, ema_fast=148.0, ema_slow=156.0)  # a bounce inside a downtrend
    bar(desk, "AAPL", 200.0)
    bar(desk, "AAPL", 203.0, vr=5.0)  # stocks aren't sniped
    assert not any("volume snipe" in i.reason for i in got)


def test_can_be_switched_off():
    desk = make(surge_snipe=False)
    got = ideas(desk)
    bar(desk, "SOL-USD", 150.0)
    bar(desk, "SOL-USD", 151.5, vr=4.0)
    assert not any("volume snipe" in i.reason for i in got)


def test_all_ten_strategies_are_available_and_can_be_switched():
    from fastapi.testclient import TestClient

    from tradezone.bots.quant import STRATEGIES
    from tradezone.server import create_app
    desk = make()
    assert len(STRATEGIES) == 10
    info = {x["key"]: x for x in desk.strategy_info()}
    assert {k for k, x in info.items() if x["on"]} == {"momentum", "mean_reversion", "breakout", "macd", "obv"}
    with TestClient(create_app(desk, run_desk=False)) as c:
        r = c.post("/api/strategies/bollinger", json={"on": True})
        assert r.status_code == 200 and next(x for x in r.json() if x["key"] == "bollinger")["on"]
        assert c.post("/api/strategies/momentum", json={"on": False}).status_code == 400  # the core always runs
        assert c.post("/api/strategies/nope", json={"on": True}).status_code == 400
    assert desk.store.load("strategies")["on"] == ["macd", "obv", "bollinger"]  # remembered after a restart


def test_every_strategy_scores_a_real_bar():
    from tradezone.bots.quant import STRATEGIES
    desk = make(strategies=list(STRATEGIES))
    from tradezone.core.models import Tick
    for i in range(80):  # build real indicators from ticks
        px = 150 * (1 + 0.002 * i)
        desk.bus.publish("market.tick", Tick("SOL-USD", px, 1000 + 50 * i, 1_800_000_000 + i))
        run(desk.bus.drain())
    sc = desk.bots["quant"].scores["SOL-USD"]
    assert all(k in sc for k in ("macd", "vwap", "trend", "roc", "obv", "bollinger", "stochastic"))
    assert sc["macd"] > 0 and sc["trend"] > 0 and sc["roc"] > 0 and sc["obv"] > 0  # a steady rise on rising volume
