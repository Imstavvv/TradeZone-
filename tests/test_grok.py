import asyncio
import json

import httpx
import pytest

from tests.helpers import capture, prime
from tradezone.bots.grok import GrokClient, parse_verdict
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, SignalKind
from tradezone.desk import TradingDesk
from tradezone.journal import Journal


def grok_desk(reply=None, status=200, fail_open=False):
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["authorization"] == "Bearer xai-test"
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    s = Settings(never_sell_at_loss=False, grok_api_key="xai-test", grok_fail_open=fail_open, feed="sim")
    s.risk.symbol_cooldown_s = 0
    desk = TradingDesk(s, journal=Journal(":memory:"))
    grok = desk.bots["grok"]
    grok.client = GrokClient("xai-test", s.grok_model, "https://api.x.ai/v1", 5,
                             client=httpx.AsyncClient(base_url="https://api.x.ai/v1",
                                                      headers={"Authorization": "Bearer xai-test"},
                                                      transport=httpx.MockTransport(handler)))
    return desk, calls


async def process(desk, sig):
    desk.bus.publish("signal.proposed", sig)
    await desk.bus.drain()
    await desk.bots["grok"].wait_idle()
    await desk.bus.drain()


def test_parse_verdict():
    v = parse_verdict('Sure! {"decision": "approve", "confidence": 0.8, "rationale": "trend"}')
    assert v.approved and v.confidence == 0.8
    with pytest.raises(ValueError):
        parse_verdict('{"decision": "maybe"}')
    with pytest.raises(ValueError):
        parse_verdict("no json here")


def test_settings_auto_mode():
    s = Settings(never_sell_at_loss=False)
    s.validate()
    assert s.grok_mode == "off"
    s = Settings(never_sell_at_loss=False, grok_api_key="k")
    s.validate()
    assert s.grok_mode == "veto"


def test_grok_approval_lets_trade_through():
    desk, calls = grok_desk('{"decision":"approve","confidence":0.9,"rationale":"clean breakout"}')
    assert desk.bots["grok"].mode == "veto"
    prime(desk)
    asyncio.run(process(desk, Signal("AAPL", Side.BUY, 0.6, "t", "quant")))
    assert desk.portfolio.position("AAPL").qty > 0
    assert calls and calls[0]["model"] == desk.settings.grok_model
    ctx = json.loads(calls[0]["messages"][1]["content"])
    assert ctx["proposal"]["symbol"] == "AAPL"


def test_grok_veto_blocks_trade():
    desk, _ = grok_desk('{"decision":"reject","confidence":0.7,"rationale":"overextended"}')
    prime(desk)
    rejected = capture(desk, "signal.rejected")
    asyncio.run(process(desk, Signal("AAPL", Side.BUY, 0.9, "t", "quant")))
    assert desk.portfolio.position("AAPL").is_flat
    assert "Grok veto" in rejected[0]["reason"]


def test_grok_outage_fails_closed_by_default():
    desk, _ = grok_desk(status=503)
    prime(desk)
    asyncio.run(process(desk, Signal("AAPL", Side.BUY, 0.9, "t", "quant")))
    assert desk.portfolio.position("AAPL").is_flat
    assert desk.bots["grok"].connected is False


def test_grok_outage_fail_open():
    desk, _ = grok_desk(status=503, fail_open=True)
    prime(desk)
    asyncio.run(process(desk, Signal("AAPL", Side.BUY, 0.9, "t", "quant")))
    assert desk.portfolio.position("AAPL").qty > 0


def test_exits_never_wait_for_grok():
    desk, calls = grok_desk('{"decision":"approve","confidence":0.9,"rationale":"ok"}')
    prime(desk)
    asyncio.run(process(desk, Signal("AAPL", Side.BUY, 0.9, "t", "quant")))
    n = len(calls)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 1, "stop", "portfolio", SignalKind.EXIT))
    asyncio.run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat
    assert len(calls) == n
