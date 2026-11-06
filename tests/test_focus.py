"""Growth ladder: a small balance trades smaller coins; bigger coins join as the balance grows."""

import httpx

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

SYMS = {"BTC-USD": 60_000.0, "SOL-USD": 150.0, "LINK-USD": 15.0, "DOGE-USD": 0.2, "ARB-USD": 0.8, "XRP-USD": 0.6, "PEPE-USD": 0.00001,
        "ETH-USD": 3_000.0}


def make(cash):
    s = Settings(feed="sim", symbols=list(SYMS), starting_cash=cash, loser_cap=0.0)  # (the underwater cap: own tests)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def buy(desk, sym):
    prime(desk, sym, SYMS[sym], atr=SYMS[sym] * 0.005)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym).qty, rejected


def test_coin_sizes():
    s = Settings()
    assert [s.coin_size(x) for x in ("BTC-USD", "SOL-USD", "DOGE-USD", "ARB-USD", "AAPL")] == \
        ["large", "mid", "small", "small", "large"]


def test_a_small_balance_trades_smaller_coins_only():
    desk = make(100.0)
    f = desk.focus_status()
    assert f["stage"] == 1 and f["max_positions"] is None and "DOGE-USD" in f["coins"] and "BTC-USD" not in f["coins"]
    qty, rej = buy(desk, "BTC-USD")
    assert qty == 0 and "joins when the balance reaches $1,000" in rej[-1]["reason"]
    qty, rej = buy(desk, "LINK-USD")  # a mid-size coin waits for $250...
    assert qty == 0 and "$250" in rej[-1]["reason"]
    assert buy(desk, "DOGE-USD")[0] > 0


def test_sol_and_xrp_are_traded_at_any_balance_and_still_stacked():
    desk = make(100.0)
    assert desk.settings.always_trade == ["SOL", "XRP"]
    assert {"SOL", "XRP"} <= set(desk.settings.stack_coins) and not desk.settings.is_stack_only("SOL-USD")
    assert desk.in_focus("SOL-USD") is None and desk.in_focus("XRP-USD") is None
    assert desk.in_focus("LINK-USD") is not None  # (other mid-size coins still follow the ladder)
    assert buy(desk, "SOL-USD")[0] > 0
    assert buy(desk, "XRP-USD")[0] > 0


def test_always_traded_coins_are_always_watched():
    s = Settings(feed="coinbase", symbols=["BTC-USD", "DOGE-USD"])
    s.validate()
    assert "SOL-USD" in s.symbols and "XRP-USD" in s.symbols


def test_a_small_balance_can_hold_more_than_four_trades_now():
    desk = make(200.0)
    SYMS["WIF-USD"], SYMS["BONK-USD"] = 2.0, 0.00003
    desk.settings.symbols += ["WIF-USD", "BONK-USD"]
    desk.settings.max_meme_positions = 5
    for sym in ("DOGE-USD", "ARB-USD", "PEPE-USD", "WIF-USD", "BONK-USD"):
        assert buy(desk, sym)[0] > 0  # (it used to stop at 4 at this size)


def test_up_to_100_trades_at_once_and_never_more():
    from tradezone.core.portfolio import Position
    desk = make(100_000.0)
    assert desk.settings.risk.max_open_positions == 100
    for lvl in ("aggressive", "bold"):
        desk.settings.risk_profile = lvl
        assert desk.settings.limits(True).max_open_positions == 100
    desk.settings.risk_profile = "normal"
    for i in range(100):
        desk.portfolio.positions[f"C{i}-USD"] = Position(f"C{i}-USD", qty=1.0, avg_price=1.0, last_price=1.0)
    qty, rej = buy(desk, "DOGE-USD")
    assert qty == 0 and "max open positions" in rej[-1]["reason"]


def test_bigger_coins_join_as_the_balance_grows():
    mid = make(500.0)
    assert mid.focus_status()["stage"] == 2 and buy(mid, "SOL-USD")[0] > 0 and buy(mid, "ETH-USD")[0] == 0
    big = make(5_000.0)
    assert big.focus_status()["stage"] == 3 and buy(big, "BTC-USD")[0] > 0 and buy(big, "ETH-USD")[0] > 0


def test_kraken_feed_adds_small_altcoins_that_trade_enough():
    from tradezone.market.kraken import KrakenFeed
    pairs = {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT"},
             "ARBUSD": {"altname": "ARBUSD", "wsname": "ARB/USD", "base": "ARB"},
             "QUIETUSD": {"altname": "QUIETUSD", "wsname": "QUIET/USD", "base": "QUIET"}}
    vol = {"ARBUSD": 5e7, "QUIETUSD": 10.0, "XBTUSD": 1e3}

    def handler(req):
        if req.url.path.endswith("AssetPairs"):
            return httpx.Response(200, json={"error": [], "result": pairs})
        return httpx.Response(200, json={"error": [], "result": {
            a: {"c": ["1", "1"], "b": ["0.999", "1", "1"], "a": ["1", "1", "1"], "v": ["1", str(vol[a])], "p": ["1", "1"]}
            for a in req.url.params["pair"].split(",")}})
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)),
                      small_candidates=["ARB", "QUIET", "NOPE"], min_meme_volume_usd=1e6)
    run(feed.start())
    assert symbols == ["BTC-USD", "ARB-USD"] and feed.added == ["ARB-USD"]


def test_the_ladder_can_be_switched_off():
    desk = make(100.0)
    desk.settings.focus_small_coins = False
    assert desk.focus_status()["stage"] == 3 and desk.in_focus("BTC-USD") is None
    assert buy(desk, "BTC-USD")[0] > 0


def test_up_to_five_meme_coins_at_once():
    from tradezone.core.portfolio import Position
    desk = make(100_000.0)
    held = ["WIF-USD", "BONK-USD", "FLOKI-USD", "SHIB-USD"]
    assert all(desk.settings.is_meme(m) for m in held) and desk.settings.max_meme_positions == 5
    for m in held:
        desk.portfolio.positions[m] = Position(m, qty=1.0, avg_price=1.0, last_price=1.0)
    assert buy(desk, "DOGE-USD")[0] > 0  # the fifth meme coin is fine
    qty, rej = buy(desk, "PEPE-USD")
    assert qty == 0 and "most meme coins" in rej[-1]["reason"]  # a sixth waits
