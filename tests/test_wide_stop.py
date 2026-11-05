"""-33% stop-loss from the buy price: small dips never stop a trade out; winners stay protected."""

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(**kw):
    s = Settings(never_sell_at_loss=False, feed="sim", symbols=["BTC-USD", "ETH-USD", "DOGE-USD"], **kw)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def buy(desk, sym="BTC-USD", price=50_000.0):
    desk.settings.skimmer = False
    prime(desk, sym, price, atr=price * 0.005)
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym)


def tick(desk, sym, price):
    desk.prices[sym] = price
    desk.broker.on_prices([Tick(sym, price)])
    desk.bus.publish("market.tick", Tick(sym, price))
    run(desk.bus.drain())


def test_the_stop_is_33_percent_below_the_buy_price():
    desk = make()
    pos = buy(desk)
    assert pos.stop_price == pytest.approx(pos.avg_price * 0.67, rel=1e-3)
    meme = buy(desk, "DOGE-USD", 0.2)
    assert meme.stop_price == pytest.approx(meme.avg_price * 0.67, rel=1e-3)  # meme coins too


def test_small_and_medium_dips_dont_stop_the_trade_out():
    desk = make()
    pos = buy(desk)
    for p in (49_000, 47_500, 45_000, 40_000, 36_000):  # down 2%, 5%, 10%, 20%, 28%
        tick(desk, "BTC-USD", p)
        assert pos.qty > 0
    trades = capture(desk, "trade.closed")
    tick(desk, "BTC-USD", 33_000)  # -34%: through the stop
    assert pos.is_flat and trades[-1].pnl < 0


def test_sized_by_the_stop_a_full_stop_out_costs_only_one_trades_risk():
    desk = make(full_cash=False)
    eq = desk.portfolio.equity
    pos = buy(desk)
    at_risk = (pos.avg_price - pos.stop_price) * pos.qty
    assert at_risk <= eq * desk.settings.risk.risk_per_trade * 1.01  # a smaller position, the same money at risk


def test_full_cash_sizes_for_the_patient_cut_so_the_whole_balance_can_trade():
    desk = make()
    eq = desk.portfolio.equity
    pos = buy(desk)
    assert pos.stop_price == pytest.approx(pos.avg_price * 0.67, rel=1e-3)  # the -33% stop is still there
    lost_at_cut = pos.qty * pos.avg_price * desk.settings.patient_stop_pct
    assert lost_at_cut <= eq * desk.settings.risk.risk_per_trade * 1.01  # one trade's risk at the patient cut...
    old = make(full_cash=False)
    assert pos.qty > 3 * buy(old).qty  # ...so far more of the cash is at work
    assert pos.qty * pos.avg_price <= eq * desk.settings.risk.max_position_pct * 1.001  # still within the position cap


def test_full_cash_never_spends_more_than_the_cash_and_never_borrows():
    desk = make()
    desk.settings.risk.max_position_pct = 1.0
    desk.settings.risk.max_portfolio_heat = 1.0
    desk.settings.risk.risk_per_trade = 0.5
    pos = buy(desk)
    assert desk.portfolio.cash >= 0 and pos.qty * pos.avg_price <= desk.settings.starting_cash


def test_winners_are_still_protected_from_the_normal_plan():
    desk = make()
    pos = buy(desk)
    entry = pos.avg_price
    tick(desk, "BTC-USD", entry * 1.04)  # +4%: past 1R of the normal -3% plan
    assert pos.stop_price >= entry  # locked at break-even + costs or better, not left at -33%
    tick(desk, "BTC-USD", entry * 1.10)
    assert pos.stop_price >= entry * 1.05  # trailing / locking as before


def test_it_can_be_switched_off():
    pos = buy(make(wide_stop=False))
    assert pos.stop_price == pytest.approx(pos.avg_price * 0.97, rel=1e-3)
