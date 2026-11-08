"""The Trades tab: every trade from buy to sale, built from its fills, with the result after all fees."""

import pytest
from fastapi.testclient import TestClient

from tradezone.config import Settings
from tradezone.core.portfolio import Position
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app

T0 = 1_800_000_000.0


def fill(j, ts, sym, side, action, qty, price, after, pnl=None, fee=0.1, reason="", mode="paper"):
    j.record(T0 + ts, "trade.log", "portfolio", {"ts": T0 + ts, "mode": mode, "symbol": sym, "side": side, "action": action,
                                                  "qty": qty, "price": price, "fee": fee, "pnl": pnl, "position_after": after,
                                                  "reason": reason}, mode)


def test_a_trade_bought_twice_and_sold_in_two_parts_is_one_card():
    j = Journal(":memory:")
    fill(j, 0, "SOL-USD", "buy", "open", 2, 100, 2, reason="breakout lead in trending regime")
    fill(j, 60, "SOL-USD", "buy", "add", 2, 90, 4, reason="dip buy: 6% below its average")
    fill(j, 600, "SOL-USD", "sell", "partial", 1, 104, 3, pnl=8.5, reason="skimmer: banked 25% halfway to the target")
    fill(j, 3600, "SOL-USD", "sell", "close", 3, 110, 0, pnl=44.0, reason="take-profit +15%")
    r = j.round_trips("paper", now=T0 + 4000)
    assert r["total"] == 1
    t = r["trips"][0]
    assert t["status"] == "closed" and t["side"] == "long" and t["adds"] == 1 and t["sold_parts"] == 2
    assert t["avg_entry"] == pytest.approx(95.0) and t["avg_exit"] == pytest.approx((104 + 330) / 4)
    assert t["pnl"] == pytest.approx(52.5) and t["pct"] == pytest.approx(52.5 / 380)
    assert t["fees"] == pytest.approx(0.4) and t["held_s"] == 3600
    assert t["entry_reason"].startswith("breakout") and t["exit_reason"] == "take-profit +15%"
    assert [x["kind"] for x in t["legs"]] == ["buy", "add", "sell", "sell"]


def test_open_trades_show_where_they_stand_and_a_move_to_the_hold_stack_isnt_a_sale():
    j = Journal(":memory:")
    fill(j, 0, "ETH-USD", "buy", "open", 1, 3000, 1)
    fill(j, 0, "XRP-USD", "buy", "open", 100, 0.6, 100)
    r = j.round_trips("paper", held={"ETH-USD": {"qty": 1, "last_price": 3100, "unrealized_pnl": 100, "net": 92}}, now=T0 + 60)
    by = {t["symbol"]: t for t in r["trips"]}
    assert by["ETH-USD"]["status"] == "open" and by["ETH-USD"]["net_now"] == 92
    assert by["XRP-USD"]["status"] == "moved"  # not held any more, never sold: moved to the hold stack
    assert r["stats"]["closed"] == 0 and r["stats"]["open"] == 1
    assert j.round_trips("paper")["trips"][0]["status"] == "open"  # (holdings unknown: shown open)


def test_every_trade_is_a_purchase_never_a_short():
    """No shorting: a sale with no recorded buy (coins taken over or moved out of the hold stack) is a sale."""
    j = Journal(":memory:")
    fill(j, 0, "BTC-USD", "sell", "close", 0.01, 61_000, 0, pnl=10.0)
    fill(j, 60, "ETH-USD", "buy", "open", 1, 3_000, 1)
    fill(j, 120, "ETH-USD", "sell", "close", 1, 3_100, 0, pnl=95.0)
    r = j.round_trips("paper", now=T0 + 200)
    assert {t["side"] for t in r["trips"]} == {"long"}
    assert all(leg["kind"] in ("buy", "add", "sell") for t in r["trips"] for leg in t["legs"])


def test_the_totals_and_the_period():
    j = Journal(":memory:")
    for i, (pnl, px) in enumerate([(30.0, 115), (-10.0, 95), (5.0, 102)]):
        fill(j, i * 1000, "BTC-USD", "buy", "open", 1, 100, 1)
        fill(j, i * 1000 + 500, "BTC-USD", "sell", "close", 1, px, 0, pnl=pnl)
    fill(j, 2500, "PEPE-USD", "buy", "open", 1e6, 1e-5, 1e6)
    fill(j, 2600, "PEPE-USD", "sell", "partial", 5e5, 1.2e-5, 5e5, pnl=0.9)  # banked while still open
    s = j.round_trips("paper", held={"PEPE-USD": {"qty": 5e5, "last_price": 1.1e-5}}, now=T0 + 3000)["stats"]
    assert (s["closed"], s["wins"], s["losses"]) == (3, 2, 1) and s["win_rate"] == pytest.approx(2 / 3)
    assert s["net"] == pytest.approx(25.0) and s["best"] == 30.0 and s["worst"] == -10.0
    assert s["avg_win"] == pytest.approx(17.5) and s["profit_factor"] == pytest.approx(3.5)
    assert s["banked_open"] == pytest.approx(0.9) and s["avg_hold_s"] == 500
    recent = j.round_trips("paper", since=T0 + 1800, held={}, now=T0 + 3000)
    assert recent["stats"]["closed"] == 1 and {t["symbol"] for t in recent["trips"]} == {"BTC-USD", "PEPE-USD"}
    assert j.round_trips("live")["total"] == 0  # practice and real money are separate


def test_the_endpoint_for_the_main_account_and_a_wrong_one():
    s = Settings(feed="sim", symbols=["BTC-USD"])
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    fill(d.journal, 0, "BTC-USD", "buy", "open", 0.01, 60_000, 0.01)
    d.paper_portfolio.positions["BTC-USD"] = Position(symbol="BTC-USD", qty=0.01, avg_price=60_000, last_price=61_000)
    with TestClient(create_app(d, run_desk=False)) as c:
        r = c.get("/api/trades/trips").json()
        assert r["mode"] == "paper" and r["account"] == "main" and r["trips"][0]["status"] == "open"
        assert r["trips"][0]["unrealized"] == pytest.approx(10.0)
        assert c.get("/api/trades/trips?account=nope").status_code == 404


def test_coins_moved_out_of_the_hold_stack_and_sold_are_a_normal_trade_not_a_short():
    """The move isn't a fill, so the first record is a sale: it used to show as a short bought at the first sale's
    price, and that first sale's profit was dropped."""
    j = Journal(":memory:")
    fill(j, 100, "SOL-USD", "sell", "partial", 0.1, 160, 0.1, pnl=1.6)
    fill(j, 200, "SOL-USD", "sell", "close", 0.1, 165, 0, pnl=2.1)
    t = j.round_trips("paper", held={})["trips"][0]
    assert (t["side"], t["status"], t["sold_parts"]) == ("long", "closed", 2)
    assert t["pnl"] == pytest.approx(3.7) and t["qty"] == pytest.approx(0.2)
    assert t["avg_entry"] == pytest.approx(144.0) and t["entry_reason"] == "moved out of the hold stack"


def test_the_track_record_is_measured_on_profit_not_on_deposits():
    """How long it has traded, its biggest drop and its longest time under water (the leaderboard measures)."""
    j = Journal(":memory:")
    h = 3600
    # $1,000 in; profit +100, then down to +40 (a drop), back to a new high at +120 two days later; then a $500 deposit
    for i, (eq, pnl) in enumerate([(1000, 0), (1100, 100), (1040, 40), (1060, 60), (1120, 120), (1620, 120)]):
        j.record_equity(T0 + i * 24 * h, eq, "paper", pnl)
    r = j.track_record("paper", now=T0 + 5 * 24 * h)
    assert round(r["days"]) == 5
    assert abs(r["max_drawdown"] - 60 / 1100) < 1e-9  # the drop from +100 to +40, on $1,100 (the deposit isn't a gain)
    assert round(r["longest_recovery_s"] / 86_400) == 3 and r["underwater_s"] == 0


def test_the_tax_report_matches_sales_to_buys_first_in_first_out_with_fees():
    j = Journal(":memory:")
    fill(j, 0, "SOL-USD", "buy", "open", 1, 100, 1, fee=1.0)
    fill(j, 60, "SOL-USD", "buy", "add", 1, 200, 2, fee=1.0)
    fill(j, 120, "SOL-USD", "sell", "partial", 1.5, 300, 0.5, pnl=0, fee=1.5)
    fill(j, 180, "XRP-USD", "buy", "open", 10, 2, 10, fee=0.0, reason="taken over from your Kraken balance")
    fill(j, 240, "XRP-USD", "sell", "close", 10, 3, 0, pnl=10, fee=0.0)
    g = j.gains("paper")
    sol, xrp = g
    assert (sol["cost"], sol["proceeds"], sol["gain"]) == (201.5, 448.5, 247.0)  # 1 x 101 + 0.5 x 201, fees in
    assert sol["term"] == "short" and sol["note"] == ""
    assert xrp["gain"] == 10.0 and "taken over" in xrp["note"]  # (cost = the price that day: noted)
    assert j.gains("paper", year=1999) == []


def test_the_tax_report_downloads_as_csv():
    from fastapi.testclient import TestClient

    from tradezone.config import Settings
    from tradezone.desk import TradingDesk
    from tradezone.server import create_app
    d = TradingDesk(Settings(feed="sim", symbols=["SOL-USD"]), journal=Journal(":memory:"))
    fill(d.journal, 0, "SOL-USD", "buy", "open", 1, 100, 1, fee=1.0, mode="live")
    fill(d.journal, 60, "SOL-USD", "sell", "close", 1, 120, 0, pnl=18, fee=1.0, mode="live")
    with TestClient(create_app(d, run_desk=False)) as c:
        r = c.get("/api/reports/gains.csv?mode=live")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("description,qty,acquired,sold,proceeds,cost,gain") and "1 SOL" in lines[1] and ",18.0," in lines[1]
