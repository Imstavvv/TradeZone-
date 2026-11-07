import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Regime, Side, Signal, Snapshot, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def snap(price=100.0, rsi=50.0, ema_fast=100.0, ema_slow=100.0, hi=200.0, lo=1.0):
    ind = {"ema_fast": ema_fast, "ema_slow": ema_slow, "sma50": price, "rsi": rsi, "volatility": 0.01,
           "atr": 0.5, "donchian_high": hi, "donchian_low": lo}
    return Snapshot("AAPL", price, ind)


def push(desk, s):
    desk.bus.publish("market.snapshot", s)
    run(desk.bus.drain())


# ---------------------------------------------------------------- consensus
def test_votes_and_vetoes(desk):
    q = desk.bots["quant"]
    up = {"momentum": 0.8, "mean_reversion": 0.0, "breakout": 1.0}
    assert q.consensus(1, up, Regime.TRENDING) == {
        "momentum": "yes", "mean_reversion": "abstain", "breakout": "yes", "strategist": "yes"}
    assert q.consensus(1, up, Regime.VOLATILE)["strategist"] == "no"
    conflict = {"momentum": 0.9, "mean_reversion": -0.6, "breakout": 0.0}
    assert q.consensus(1, conflict, Regime.UNKNOWN)["mean_reversion"] == "no"


def test_buy_blocked_when_a_bot_disagrees(desk):
    signals = capture(desk, "signal.proposed")
    # strong momentum but overbought RSI: momentum says buy, mean reversion objects
    push(desk, snap(price=101, rsi=80, ema_fast=103, ema_slow=100, hi=100.5))
    assert signals == []
    assert desk.bots["quant"].metrics["consensus_blocked"] == 1


def test_buy_proceeds_when_team_agrees(desk):
    desk.settings.consensus_voters = "families"  # (the 4-voter family vote these tests are about)
    signals = capture(desk, "signal.proposed")
    # momentum up, breakout up, RSI neutral (abstain), regime unknown (abstain) -> 2 yes, 0 no
    push(desk, snap(price=101, rsi=55, ema_fast=103, ema_slow=100, hi=100.5))
    assert len(signals) == 1 and signals[0].side is Side.BUY
    assert list(signals[0].votes.values()).count("yes") >= 2 and "no" not in signals[0].votes.values()


@pytest.mark.parametrize("share,trades", [(0.25, True), (0.5, True), (0.75, False), (1.0, False)])
def test_the_share_of_voters_that_must_agree(desk, share, trades):
    desk.settings.consensus_voters = "families"  # (the 4-voter family vote these tests are about)
    """4 voters; this setup gets 2 yes (momentum, breakout) and 2 abstain: 25% and 50% let it through, 75% / 100% don't."""
    desk.settings.risk.consensus_min_share = share
    signals = capture(desk, "signal.proposed")
    push(desk, snap(price=101, rsi=55, ema_fast=103, ema_slow=100, hi=100.5))
    assert (len(signals) == 1) is trades
    if not trades:
        assert desk.bots["quant"].metrics["consensus_blocked"] == 1


def test_by_default_the_old_count_rule_applies(desk):
    assert desk.settings.risk.consensus_min_share == 0.0 and desk.settings.risk.consensus_min_yes == 2


def test_consensus_rechecked_while_stance_holds(desk):
    desk.settings.consensus_voters = "families"  # (the 4-voter family vote these tests are about)
    signals = capture(desk, "signal.proposed")
    push(desk, snap(price=101, rsi=80, ema_fast=103, ema_slow=100, hi=100.5))  # blocked
    push(desk, snap(price=101, rsi=55, ema_fast=103, ema_slow=100, hi=100.5))  # now agreed
    assert len(signals) == 1


# ---------------------------------------------------------- profit protection
def open_long(desk, price=100.0):
    prime(desk, price=price)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("AAPL")
    assert pos.qty > 0
    return pos


def tick(desk, px):
    desk.prices["AAPL"] = px
    desk.broker.on_prices([Tick("AAPL", px)])
    desk.bus.publish("market.tick", Tick("AAPL", px))
    run(desk.bus.drain())


def test_green_trade_cannot_close_red(desk):
    desk.settings.scale_out_fraction = 0  # isolate the break-even lock
    pos = open_long(desk)
    entry, r = pos.avg_price, desk.bots["portfolio"].trail_dist["AAPL"]
    tick(desk, entry + 0.6 * r)  # in profit beyond the trigger
    assert "AAPL" in desk.bots["portfolio"].protected
    assert pos.stop_price > entry + desk.bots["portfolio"].round_trip_cost(entry)
    trades = capture(desk, "trade.closed")
    tick(desk, pos.stop_price - 0.001)  # price falls back to the protective stop
    assert desk.portfolio.position("AAPL").is_flat
    assert trades[0].pnl > 0
    assert desk.bots["portfolio"].metrics.get("winners_turned_losers", 0) == 0


def test_partial_take_profit_at_1r(desk):
    desk.settings.stop_method = "structure"  # with percent stops the partial happens at the target
    desk.settings.skimmer = False  # this checks the Portfolio Manager's own partial
    pos = open_long(desk)
    q0, r = pos.qty, desk.bots["portfolio"].trail_dist["AAPL"]
    trades = capture(desk, "trade.closed")
    tick(desk, pos.avg_price + 1.05 * r + 0.2)
    assert 0 < pos.qty < q0 and pos.qty == pytest.approx(q0 / 2, rel=0.01)
    assert trades and trades[0].pnl > 0
    tick(desk, pos.avg_price + 1.2 * r)  # no second partial
    assert desk.bots["portfolio"].metrics["partial_profits"] == 1


def test_losing_trade_still_cut_by_stop(desk):
    pos = open_long(desk)
    trades = capture(desk, "trade.closed")
    tick(desk, pos.stop_price * 0.999)
    assert desk.portfolio.position("AAPL").is_flat and trades[0].pnl < 0


def test_strategy_exit_only_in_profit(desk):
    open_long(desk)
    rejected = capture(desk, "signal.rejected")
    tick(desk, 99.8)  # small loss, above the stop
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 0.9, "bearish", "quant"))
    run(desk.bus.drain())
    assert "not in profit" in rejected[-1]["reason"]
    assert desk.portfolio.position("AAPL").qty > 0
    tick(desk, 100.6)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 0.9, "bearish", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat


def test_operator_flatten_ignores_profit_rule(desk):
    open_long(desk)
    tick(desk, 99.8)
    desk.flatten("operator")
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat


# ------------------------------------------------------------- bar building
def test_scout_builds_bars_from_live_ticks():
    s = Settings(never_sell_at_loss=False, feed="coinbase", bar_seconds=10)
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    scout = desk.bots["scout"]
    snaps = capture(desk, "market.snapshot")
    for i in range(400):  # 400 one-second ticks -> 39 closed 10s bars
        desk.bus.publish("market.tick", Tick("BTC-USD", 100 + (i % 7), 1, 1_000_000 + i))
    run(desk.bus.drain())
    assert scout.metrics["bars"] == 39
    assert len(scout.history("BTC-USD")) == 39
    assert len(snaps) == 39 - 29  # snapshots start after the 30-bar warmup
    assert desk.prices["BTC-USD"] == 100 + 399 % 7  # prices stay tick-fresh for stops


def test_six_bots_must_agree_on_a_buy(desk):
    """Every strategy with an opinion and the Strategist vote; at least 6 yes, more yes than no (asked for)."""
    assert Settings().consensus_voters == "families"  # (the old vote is back: the 6-bot vote did worse on unseen data)
    desk.settings.consensus_voters = "strategies"
    assert desk.settings.consensus_bots == 6
    signals = capture(desk, "signal.proposed")
    push(desk, snap(price=101, rsi=55, ema_fast=103, ema_slow=100, hi=100.5))  # only a few strategies say yes
    assert not [x for x in signals if x.source == "quant"]
    why = next(iter(desk.bots["quant"].blocked.values()), "")
    assert "bots agree" in why and "6 needed" in why
    desk.settings.consensus_bots = 2  # (a lower bar: the same idea goes through)
    push(desk, snap(price=101.2, rsi=55, ema_fast=103.2, ema_slow=100, hi=100.5))
    sent = [x for x in signals if x.source == "quant"]
    assert sent and "bots agree" in sent[-1].reason
