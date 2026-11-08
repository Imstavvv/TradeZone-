"""The Meme desk (asked for): TradeZone's own bot team that trades only meme coins, on practice money to start."""

from fastapi.testclient import TestClient

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app


def main_desk(db=":memory:"):
    s = Settings(feed="sim", symbols=["BTC-USD", "DOGE-USD"])
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))


def test_the_meme_desk_trades_meme_coins_only_on_practice_money():
    d = main_desk()
    row = d.fleet.add_meme_desk()
    md = d.fleet.desks[row["id"]]
    assert row["label"] == "🐸 Meme desk" and md.settings.meme_only and not md.is_live
    assert md.settings.symbols and all(md.settings.is_meme(x) for x in md.settings.symbols)
    assert d.fleet.add_meme_desk()["id"] == row["id"]  # (one Meme desk)
    md.settings.symbols.append("BTC-USD")
    md.settings.risk.symbol_cooldown_s = 0
    for sym, px in (("BTC-USD", 50_000.0), ("DOGE-USD", 0.2)):
        prime(md, sym, px, atr=px * 0.005)
    rejected = capture(md, "signal.rejected")
    md.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.95, "t", "quant"))
    run(md.bus.drain())
    assert md.portfolio.position("BTC-USD").is_flat and "meme coins only" in rejected[-1]["reason"]
    md.bus.publish("signal.proposed", Signal("DOGE-USD", Side.BUY, 0.95, "t", "quant"))
    run(md.bus.drain())
    assert md.portfolio.position("DOGE-USD").qty > 0


def test_the_meme_desk_comes_back_after_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    d = main_desk(db)
    acct = d.fleet.add_meme_desk()["id"]
    d.fleet.desks[acct].save_state()
    d2 = main_desk(db)
    d2.fleet.load()
    assert acct in d2.fleet.desks and d2.fleet.desks[acct].settings.meme_only


def test_started_from_the_accounts_page():
    d = main_desk()
    with TestClient(create_app(d, run_desk=False)) as c:
        r = c.post("/api/accounts/meme-desk")
        assert r.status_code == 200 and r.json()["account"]["label"] == "🐸 Meme desk"
        assert any(a["label"] == "🐸 Meme desk" for a in d.state()["accounts"])  # (its own tab, like an account)
