"""The Alpaca team: US stocks next to its crypto, no shorting by default, and the US day-trade rule.

Under $25,000 an account may make at most 3 day trades (buy and sell the same stock the same day) in 5
trading days - Alpaca refuses the 4th, and repeat breaches lock the account. The bots stop at 2, keeping
the last one for your own Close all / Emergency stop."""

import time

from tests.helpers import capture, run
from tradezone.config import ALPACA_STOCKS, Settings
from tradezone.core.models import Side, Signal, SignalKind
from tradezone.core.portfolio import Position
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(db=":memory:", cash=5_000.0, **kw):
    s = Settings(feed="sim", symbols=["AAPL", "SPY", "BTC-USD"], starting_cash=cash, live_venue="alpaca",
                 alpaca_live_key="k", alpaca_live_secret="s", loser_cap=0.0, **kw)
    s.risk.symbol_cooldown_s = 0
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    d.prices.update({"AAPL": 200.0, "SPY": 450.0, "BTC-USD": 60_000.0})
    return d


def trade(desk, sym, action, ts=None):
    desk.bus.publish("trade.log", {"symbol": sym, "action": action, "mode": desk.mode, "ts": ts or time.time(),
                                   "price": desk.prices[sym], "qty": 1.0,
                                   "side": "buy" if action in ("open", "add") else "sell", "pnl": None})
    run(desk.bus.drain())


def hold(desk, sym, qty=2.0, avg=200.0):
    desk.portfolio.positions[sym] = Position(symbol=sym, qty=qty, avg_price=avg, last_price=desk.prices[sym] * 1.1)
    desk.prices[sym] *= 1.1  # in profit: nothing else stops the sale


def exit_reason(desk, sym, force=False):
    out = capture(desk, "signal.rejected")
    desk.bots["risk"].evaluate(Signal(sym, Side.SELL, 0.99, "test", "quant", kind=SignalKind.EXIT, force=force))
    run(desk.bus.drain())
    return out[-1]["reason"] if out else ""


def test_the_alpaca_team_trades_the_tested_stocks_next_to_its_crypto_and_never_shorts_by_default():
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s")
    s.validate()
    assert [x for x in s.symbols if "-" not in x] == ALPACA_STOCKS and "BTC-USD" in s.symbols
    assert s.shorting == "off"
    d = make()
    assert "shorting is off" in (d.short_block("AAPL") or "")


def test_round_trips_on_the_same_day_are_counted_and_remembered(tmp_path):
    db = str(tmp_path / "tz.db")
    d = make(db)
    trade(d, "AAPL", "open")
    trade(d, "AAPL", "close")  # same day: a day trade
    trade(d, "BTC-USD", "open")
    trade(d, "BTC-USD", "close")  # crypto: not covered by the rule
    trade(d, "SPY", "open", ts=time.time() - 3 * 86_400)
    trade(d, "SPY", "close")  # bought days ago: not a day trade
    assert d.day_trades_used() == 1
    d2 = make(db)
    run(d2.start())
    assert d2.day_trades_used() == 1
    run(d2.shutdown())


def test_at_two_day_trades_a_stock_bought_today_waits_for_tomorrow():
    d = make()
    for _ in range(2):
        trade(d, "AAPL", "open")
        trade(d, "AAPL", "close")
    trade(d, "SPY", "open")  # bought today
    hold(d, "SPY", avg=450.0)
    why = exit_reason(d, "SPY")
    assert "day-trade rule: 2 of 3" in why and "next trading day" in why
    assert d.day_trade_status() == {"applies": True, "used": 2, "limit": 3, "bots": 2}


def test_your_own_close_all_still_sells_and_older_trades_and_crypto_are_free_to_sell():
    d = make()
    for _ in range(2):
        trade(d, "AAPL", "open")
        trade(d, "AAPL", "close")
    trade(d, "SPY", "open")
    hold(d, "SPY", avg=450.0)
    assert "day-trade" not in exit_reason(d, "SPY", force=True)  # Close all / Emergency stop
    trade(d, "AAPL", "open", ts=time.time() - 3 * 86_400)  # bought on an earlier day
    hold(d, "AAPL")
    assert "day-trade" not in exit_reason(d, "AAPL")
    hold(d, "BTC-USD", qty=0.01, avg=60_000.0)
    assert "day-trade" not in exit_reason(d, "BTC-USD")


def test_the_rule_doesnt_apply_from_25000_or_away_from_alpaca():
    d = make(cash=30_000.0)
    for _ in range(3):
        trade(d, "AAPL", "open")
        trade(d, "AAPL", "close")
    trade(d, "SPY", "open")
    assert d.day_trade_block("SPY") is None and not d.day_trade_status()["applies"]
    k = TradingDesk(Settings(feed="sim", symbols=["AAPL"], live_venue="kraken"), journal=Journal(":memory:"))
    assert k.day_trade_block("AAPL") is None


def test_alpacas_own_count_is_used_when_it_is_higher(monkeypatch):
    d = make()
    monkeypatch.setattr(TradingDesk, "is_live", property(lambda self: True))
    d.live_broker = type("B", (), {"day_trades": 2})()
    assert d.day_trades_used("live") == 2
