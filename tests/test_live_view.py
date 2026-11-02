"""Real-time bot activity, per-bot logs and the permanent trade log."""

import csv
import io

from fastapi.testclient import TestClient

from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app
from tests.helpers import capture, prime, run


def round_trip(desk, sym="AAPL"):
    prime(desk, sym)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "momentum test", "quant"))
    run(desk.bus.drain())
    stop = desk.portfolio.position(sym).stop_price
    desk.prices[sym] = stop * 0.99
    desk.broker.on_prices([Tick(sym, stop * 0.99)])
    desk.bus.publish("market.tick", Tick(sym, stop * 0.99))
    run(desk.bus.drain())


def test_every_fill_is_logged_with_reason_and_pnl(desk):
    logs = capture(desk, "trade.log")
    round_trip(desk)
    assert [t["action"] for t in logs] == ["open", "close"]
    o, c = logs
    assert o["mode"] == "paper" and o["side"] == "buy" and "momentum test" in o["reason"] and o["pnl"] is None
    assert c["pnl"] < 0 and "stop hit" in c["reason"] and c["position_after"] == 0
    stored = desk.journal.trades()
    assert stored["total"] == 2 and stored["items"][0]["action"] == "close"  # newest first
    assert stored["realized_pnl"] < 0 and stored["closing_fills"] == 1


def test_trade_log_filters_and_paging(desk):
    round_trip(desk, "AAPL")
    round_trip(desk, "NVDA")
    j = desk.journal
    assert j.trades(symbol="NVDA")["total"] == 2
    assert j.trades(mode="live")["total"] == 0
    page = j.trades(limit=3, offset=0)["items"] + j.trades(limit=3, offset=3)["items"]
    assert len(page) == 4 and len({t["fill_id"] for t in page}) == 4


def test_bot_actions_reach_ui_listeners_with_throttle(desk):
    got = []
    desk.ui_listeners.append(got.append)
    for i in range(50):
        desk.bots["quant"].act(f"thinking {i}")
    assert got and got[0]["type"] == "action" and got[0]["bot"] == "quant" and got[0]["title"] == "Quant"
    assert len(got) == desk.UI_MAX_PER_BOT_PER_SEC  # a busy bot can't flood the page
    st = desk.bots["quant"].status()
    assert st.total_actions == 50 and st.actions_per_min == 50


def test_api_trades_csv_and_bot_log():
    desk = TradingDesk(Settings(feed="sim", api_token="tok"), journal=Journal(":memory:"))
    desk.settings.risk.symbol_cooldown_s = 0
    round_trip(desk)
    hdr = {"Authorization": "Bearer tok"}
    with TestClient(create_app(desk, run_desk=False)) as c:
        d = c.get("/api/trades?symbol=AAPL", headers=hdr).json()
        assert d["total"] == 2 and d["items"][0]["symbol"] == "AAPL"
        assert c.get("/api/trades").status_code == 401
        assert c.get("/api/trades.csv").status_code == 401
        assert c.get("/api/trades.csv?token=tok").status_code == 401  # never in URLs
        assert c.post("/api/login", json={"token": "tok"}).status_code == 200  # session cookie
        rows = list(csv.reader(io.StringIO(c.get("/api/trades.csv").text)))
        assert rows[0][:4] == ["time_utc", "mode", "symbol", "side"] and len(rows) == 3
        log = c.get("/api/bots/risk/log", headers=hdr).json()
        assert log["title"] == "Risk Manager" and any("APPROVED" in e["message"] for e in log["log"])
        assert c.get("/api/bots/nope/log", headers=hdr).status_code == 404


def test_websocket_pushes_events_and_actions_live():
    desk = TradingDesk(Settings(feed="sim"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as c:
        with c.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot" and "actions" in snap["data"]
            desk.bots["risk"].act("checking exposure")
            msg = ws.receive_json()
            assert msg == {**msg, "type": "action", "bot": "risk", "message": "checking exposure"}
            desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.5, "t", "quant"), source="quant")
            run(desk.bus.drain())
            types = set()
            for _ in range(4):
                m = ws.receive_json()
                types.add(m["type"])
                if m["type"] == "event":
                    assert m["data"]["topic"] in ("signal.proposed", "signal.rejected", "alert")
                    break
            assert "event" in types


def test_review_payload_is_plain_json(desk):
    from tradezone.core.bus import Event
    sig = Signal("AAPL", Side.BUY, 0.5, "t", "quant")
    d = Event("signal.reviewed", {"signal": sig, "approved": True}).to_dict()
    assert d["payload"]["signal"]["side"] == "buy"
