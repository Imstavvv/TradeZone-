"""The Smart Money tracker, Chart Vision (xAI), the Bull vs Bear Debate and the Rerouter."""

import json
import struct
import time
import zlib

import httpx

from tests.helpers import capture, run
from tests.test_new_bots import advice, make, snap
from tradezone.bots.chartvision import parse_read
from tradezone.bots.debate import make_case
from tradezone.bots.grok import GrokClient
from tradezone.bots.rerouter import passing
from tradezone.bots.smartmoney import whale_flow
from tradezone.core.chart_png import candle_png
from tradezone.core.models import Order, OrderRequest, OrderStatus, Side, Signal, SignalKind


# -- 🐋 Smart Money ---------------------------------------------------------------------------------------
def trades(buys, sells, t):
    return ([{"price": 100.0, "qty": q, "ts": t, "side": "buy"} for q in buys]
            + [{"price": 100.0, "qty": q, "ts": t, "side": "sell"} for q in sells]
            + [{"price": 100.0, "qty": 0.1, "ts": t, "side": "sell"} for _ in range(40)])  # small fry


def test_whale_flow_counts_only_the_big_trades():
    t = time.time()
    f = whale_flow(trades([200, 150, 100], [50], t), t - 60)
    assert f["whales"] == 4 and f["buy_usd"] == 45_000 and f["sell_usd"] == 5_000 and f["lean"] == 0.8
    assert whale_flow(trades([200], [], t - 7200), t - 1800)["whales"] == 0  # (too old)


def test_smart_money_trims_buys_into_whale_selling_and_backs_whale_buying():
    desk = make()
    sm = desk.bots["smartmoney"]
    snap(desk, "ETH-USD", 100.0)
    t = time.time()
    sm.tape["ETH-USD"] = {**whale_flow(trades([20], [300, 200], t), t - 60), "at": t}
    assert advice(desk, "smartmoney") == (None, -0.05, 0.7)
    assert advice(desk, "smartmoney", side=Side.SELL) == (None, 0.0, 1.0)  # (sells never touched)
    desk.settings.smart_money = "veto"
    veto, _, _ = advice(desk, "smartmoney")
    assert veto and "whales are selling" in veto
    sm.tape["ETH-USD"] = {**whale_flow(trades([300, 200], [20], t), t - 60), "at": t}
    assert advice(desk, "smartmoney") == (None, 0.05, 1.1)
    desk.settings.smart_money = "off"
    assert advice(desk, "smartmoney") == (None, 0.0, 1.0)


def test_smart_money_reads_heavy_volume_bars_on_any_chart():
    desk = make()
    p = 100.0
    for _ in range(4):
        p *= 1.004
        snap(desk, "SOL-USD", p, volume_ratio=4.0)
    assert desk.bots["smartmoney"].bar_flow("SOL-USD") >= 6
    assert advice(desk, "smartmoney", "SOL-USD")[1] > 0


def test_smart_money_reads_the_tape_for_coins_about_to_be_bought():
    desk = make()
    sm = desk.bots["smartmoney"]

    calls = []

    class Feed:
        name = "kraken"

        async def trades(self, sym, count=500):
            calls.append(sym)
            return trades([300], [10], time.time())

    desk.feed = Feed()
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.BUY, 0.6, "t", "quant"))
    run(desk.bus.drain())
    async def cycle():
        await sm.on_cycle()
        await sm._task

    run(cycle())
    assert calls == ["ETH-USD"] and sm.tape_lean("ETH-USD") > 0.6


def test_kraken_feed_parses_the_trade_tape():
    from tradezone.market.kraken import KrakenFeed

    def handler(req):
        assert req.url.path == "/0/public/Trades"
        return httpx.Response(200, json={"error": [], "result": {
            "XETHZUSD": [["3000.1", "2.5", 1700000000.1, "b", "m", "", 1], ["2999", "1", 1700000001.2, "s", "l", "", 2]],
            "last": "1"}})

    f = KrakenFeed(["ETH-USD"], client=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
    f.pairs = {"ETH-USD": {"pair": "XETHZUSD", "key": "XETHZUSD"}}
    f.HISTORY_GAP_S = 0
    out = run(f.trades("ETH-USD"))
    assert out == [{"price": 3000.1, "qty": 2.5, "ts": 1700000000.1, "side": "buy"},
                   {"price": 2999.0, "qty": 1.0, "ts": 1700000001.2, "side": "sell"}]


# -- 👁️ Chart Vision ----------------------------------------------------------------------------------------
def test_the_candle_chart_is_a_real_png():
    rows = [[i, 100 + i, 102 + i, 99 + i, 101 + i if i % 3 else 99.5 + i, 10 + i] for i in range(60)]
    png = candle_png(rows, 320, 200)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (320, 200)
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    assert len(raw) == h * (1 + w * 3)
    assert bytes((38, 194, 129)) in raw and bytes((235, 77, 75)) in raw  # green and red candles drawn


def test_chart_vision_reply_is_checked():
    r = parse_read('Sure: {"trend": "Bearish", "confidence": 0.82, "pattern": "lower highs", "reason": "breakdown"}')
    assert r["trend"] == "bearish" and r["confidence"] == 0.82
    for bad in ("no json", '{"trend": "moon"}'):
        try:
            parse_read(bad)
            raise AssertionError("should refuse")
        except ValueError:
            pass


def test_chart_vision_sends_the_picture_to_grok_and_uses_its_read():
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["body"] = body
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"trend": "bearish", "confidence": 0.8, "pattern": "double top", "reason": "rejected at resistance"})}}]})

    desk = make()
    desk.bots["grok"].client = GrokClient("xai-test", "grok-4", "https://api.x.ai/v1", 5,
                                          client=httpx.AsyncClient(base_url="https://api.x.ai/v1",
                                                                   transport=httpx.MockTransport(handler)))
    p = 100.0
    t0 = 1_700_000_000.0
    for i in range(60 * 45):  # 45 hours of minutes: enough 1-hour candles to draw
        p *= 1.0002 if (i // 300) % 2 else 0.9999
        desk.bots["scout"].books.setdefault("ETH-USD", desk.bots["scout"]._new_book()).tf.update(t0 + 60 * i, p, p, p, 1.0)
    snap(desk, "ETH-USD", p)
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.BUY, 0.6, "t", "quant"))
    run(desk.bus.drain())
    cv = desk.bots["chartvision"]
    content = seen["body"]["messages"][1]["content"]
    assert content[0]["type"] == "image_url" and content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert cv.read("ETH-USD")["trend"] == "bearish"
    assert advice(desk, "chartvision") == (None, -0.05, 0.8)
    desk.settings.chart_vision = "veto"
    assert "bearish" in advice(desk, "chartvision")[0]


def test_chart_vision_stays_quiet_without_a_grok_key_or_over_budget():
    desk = make()
    desk.bots["grok"].client = None
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.BUY, 0.6, "t", "quant"))
    run(desk.bus.drain())
    cv = desk.bots["chartvision"]
    assert not cv._tasks and cv.read("ETH-USD") is None
    desk.settings.chart_vision_per_hour = 2
    cv.sent.extend([time.time(), time.time()])
    assert cv.budget_left() == 0


# -- ⚖️ Debate ------------------------------------------------------------------------------------------------
BULLISH = {"tf_1h_trend": 1, "tf_4h_trend": 1, "tf_1d_trend": 1, "macd_hist": 0.2, "vwap": 99, "sma50": 98,
           "adx": 28, "pdi": 30, "mdi": 12, "supertrend_up": 1, "obv_slope": 5, "rsi": 58}
BEARISH = {"tf_1h_trend": -1, "tf_4h_trend": -1, "tf_1d_trend": -1, "macd_hist": -0.2, "vwap": 101, "sma50": 102,
           "adx": 28, "pdi": 10, "mdi": 30, "supertrend_up": 0, "obv_slope": -5, "rsi": 78, "tf_1d_range_pos": 0.2}


def test_the_debate_weighs_both_sides():
    up = make_case(BULLISH, 100.0)
    assert up["margin"] >= 5 and not up["bear"]
    down = make_case(BEARISH, 100.0, smart={"tape_lean": -0.9, "bar_flow": -7},
                     vision={"trend": "bearish", "confidence": 0.9, "pattern": "breakdown"})
    assert down["margin"] <= -5 and any("whales" in p for _, p in down["bear"])
    assert any("Grok reads the chart as bearish" in p for _, p in down["bear"])


def test_debate_advice_trims_a_losing_case_and_keeps_a_record():
    desk = make()
    snap(desk, "ETH-USD", 100.0, **BEARISH)
    assert desk.settings.debate == "show" and advice(desk, "debate") == (None, 0.0, 1.0)  # (shown, trades unchanged)
    assert desk.bots["debate"].recent()[0]["verdict"] == "bear"
    desk.settings.debate = "nudge"
    assert advice(desk, "debate") == (None, -0.06, 0.75)
    desk.settings.debate = "veto"
    assert "bear wins the debate" in advice(desk, "debate")[0]
    snap(desk, "SOL-USD", 100.0, **BULLISH)
    assert advice(desk, "debate", "SOL-USD") == (None, 0.05, 1.0)
    assert advice(desk, "debate", "SOL-USD", side=Side.SELL) == (None, 0.0, 1.0)
    rec = desk.bots["debate"].recent()[0]
    assert rec["symbol"] == "SOL-USD" and rec["verdict"] == "bull" and rec["bull"]


# -- 🔀 Rerouter ------------------------------------------------------------------------------------------
def test_only_passing_refusals_are_retried():
    assert passing("EAPI:Rate limit exceeded") and passing("EAPI:Invalid nonce")
    assert passing("EOrder:Insufficient funds") and passing("EService:Unavailable")
    assert not passing("EGeneral:Permission denied") and not passing("EQuery:Unknown asset pair")
    assert not passing("refused: never trades on margin")


def _rejected_buy(desk, reason, sym="ETH-USD"):
    sig = Signal(sym, Side.BUY, 0.7, "momentum", "quant")
    rr = desk.bots["rerouter"]
    rr.on_signal(type("E", (), {"payload": sig}))
    req = OrderRequest(sym, Side.BUY, 1.0, "momentum", sig.id)
    rr.on_approved(type("E", (), {"payload": req}))
    o = Order(sym, Side.BUY, 1.0, req.id, status=OrderStatus.REJECTED, reason=reason)
    rr.on_update(type("E", (), {"payload": o}))
    return rr, sig


def test_a_buy_refused_for_a_passing_reason_is_tried_again_as_a_fresh_signal():
    desk = make()
    rr, sig = _rejected_buy(desk, "EAPI:Rate limit exceeded")
    assert len(rr.queue) == 1
    got = capture(desk, "signal.proposed")
    run(rr.on_cycle())
    assert not got  # (waits 15 s first)
    rr.queue[0]["at"] = 0
    run(rr.on_cycle())
    run(desk.bus.drain())
    retry = [s for s in got if s.reason.startswith("rerouted")]
    assert len(retry) == 1 and retry[0].id != sig.id and retry[0].kind is SignalKind.ENTRY
    assert retry[0].symbol == "ETH-USD" and retry[0].side is Side.BUY


def test_the_rerouter_never_retries_a_hard_refusal_or_more_than_twice():
    desk = make()
    rr, _ = _rejected_buy(desk, "EGeneral:Permission denied")
    assert not rr.queue and rr.missed[-1]["retried"] is False
    for _ in range(3):
        rr.queue.clear()
        _rejected_buy(desk, "EService:Busy", "SOL-USD")
    assert len(rr.tries["SOL-USD"]) == 2 and not rr.queue  # (the third: given up)
    desk.settings.rerouter = False
    rr.tries.clear()
    _rejected_buy(desk, "EService:Busy", "XRP-USD")
    assert not rr.queue


def test_sells_are_never_rerouted():
    desk = make()
    rr = desk.bots["rerouter"]
    o = Order("ETH-USD", Side.SELL, 1.0, "r1", status=OrderStatus.REJECTED, reason="EAPI:Rate limit exceeded",
              reduce_only=True)
    rr.on_update(type("E", (), {"payload": o}))
    rr.on_signal(type("E", (), {"payload": Signal("ETH-USD", Side.SELL, 1, "stop", "pm", SignalKind.EXIT)}))
    assert not rr.queue and not rr.signals


# -- 🔍 one coin's read, on the site --------------------------------------------------------------------------
def test_the_coin_read_shows_the_debate_the_whales_and_the_chart():
    from fastapi.testclient import TestClient

    from tradezone.server import create_app

    desk = make()
    snap(desk, "ETH-USD", 100.0, **BULLISH)
    book = desk.bots["scout"].books.setdefault("ETH-USD", desk.bots["scout"]._new_book())
    for i in range(60 * 40):
        book.tf.update(1_700_000_000 + 60 * i, 100 + i * 0.01, 100 + i * 0.01, 100 + i * 0.01, 1.0)
    desk.bots["grok"].client = None
    with TestClient(create_app(desk, run_desk=False)) as c:
        r = c.get("/api/read", params={"symbol": "eth-usd"}).json()
        assert r["debate"]["verdict"] == "bull" and r["debate"]["bull"]
        assert r["chart_png"] and r["vision"] is None and r["vision_status"]["connected"] is False
        assert "tape_lean" in r["smart_money"]
        assert c.post("/api/read/look", json={"symbol": "ETH-USD"}).status_code == 400  # (no Grok key)
