"""Coach: the team rules and learning from closed trades, always inside the hard limits."""

from tests.helpers import capture, prime, run
from tradezone.bots import TEAM_RULES
from tradezone.core.models import Side, Signal, Tick, now
from tradezone.desk import TradingDesk
from tradezone.journal import Journal


def trade(desk, sym="AAPL", entry=100.0, exit_=101.0, regime="trending_up", parts=None):
    """One full round trip through the real pipeline: Quant signal -> ... -> fill -> close."""
    parts = parts or {"momentum": 0.8, "mean_reversion": 0.0, "breakout": 0.2}
    prime(desk, sym, entry)
    desk.bots["quant"].scores[sym] = {**parts, "ensemble": 0.6, "regime": regime}
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    qty = desk.portfolio.position(sym).qty
    assert qty > 0
    prime(desk, sym, exit_)
    if exit_ > entry:  # strategy exit (only allowed in profit)
        desk.bus.publish("signal.proposed", Signal(sym, Side.SELL, 0.9, "exit", "quant"))
    else:  # losers close at the safety stop
        desk.bus.publish("market.tick", Tick(sym, min(exit_, desk.portfolio.position(sym).stop_price * 0.999)))
    run(desk.bus.drain())
    assert desk.portfolio.position(sym).is_flat


def test_every_bot_has_a_mission_and_the_rules_are_shared(desk):
    assert [r["rule"] for r in TEAM_RULES] == ["Make money", "Take trades", "Grow", "Learn the markets",
                                              "The goal: grow fast, lose little", "Don't lose the money"]
    for bot in desk.bots.values():
        assert bot.mission, f"{bot.name} has no mission"
    st = desk.state()
    assert st["coach"]["rules"] == TEAM_RULES
    assert all(b["mission"] for b in st["bots"])


def test_closed_trade_is_credited_to_strategy_coin_and_mood(desk):
    trade(desk, exit_=102.0)
    coach = desk.bots["coach"]
    assert coach.learned_trades == 1
    assert coach.stats["symbol"]["AAPL"]["wins"] == 1
    assert coach.stats["regime"]["trending_up"]["n"] == 1
    strat = coach.stats["strategy"]
    assert strat["momentum"]["n"] > strat["breakout"]["n"] > 0  # credited by share of the signal
    assert "mean_reversion" not in strat  # it didn't contribute


def test_winning_strategy_earns_trust_losing_one_loses_it(desk):
    coach = desk.bots["coach"]
    for _ in range(20):
        coach._add("strategy", "momentum", 1.0, True, 50.0)
        coach._add("strategy", "breakout", 1.0, False, -50.0)
    assert 1.2 < coach.strategy_mult("momentum") <= 1.5
    assert 0.5 <= coach.strategy_mult("breakout") < 0.8
    assert coach.strategy_mult("mean_reversion") == 1.0  # no record yet, no opinion
    # a couple of lucky trades barely move it
    coach.stats["strategy"]["mean_reversion"] = {"n": 3.0, "wins": 3.0, "pnl": 10.0}
    assert coach.strategy_mult("mean_reversion") < 1.15


def test_quant_uses_learned_trust(desk):
    coach = desk.bots["coach"]
    for _ in range(30):
        coach._add("strategy", "momentum", 1.0, False, -10.0)
    from tradezone.core.models import Snapshot
    snap = Snapshot("AAPL", 100.0, {"atr": 0.5, "volatility": 0.003, "rsi": 50, "ema_fast": 100, "ema_slow": 100,
                                     "sma50": 100, "donchian_high": 100, "donchian_low": 100})
    weights_seen = {}
    q = desk.bots["quant"]
    orig = coach.strategy_mult
    coach.strategy_mult = lambda k: weights_seen.setdefault(k, orig(k))
    q.on_snapshot(type("E", (), {"payload": snap})())
    assert weights_seen["momentum"] < 0.8 and weights_seen["breakout"] == 1.0


def test_coin_that_keeps_losing_is_benched_then_returns(desk):
    coach = desk.bots["coach"]
    for _ in range(4):  # earlier losses on this coin
        coach.learn("AAPL", None, -100.0)
    assert coach.benched("AAPL") == 0  # 4 trades: not enough evidence yet
    trade(desk, exit_=98.0)  # one more real loss through the stop
    assert coach.benched("AAPL") > 0
    rejected = capture(desk, "signal.rejected")
    prime(desk, "AAPL", 100.0)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat
    assert any("benched" in str(getattr(r, "reason", r)) for r in rejected)
    assert any("benched" in le["text"] for le in coach.lessons)
    coach.bench["AAPL"] = now() - 1  # time served
    assert coach.benched("AAPL") == 0 and "AAPL" not in coach.bench


def test_grow_when_winning_shrink_when_losing_hard_limits_hold(desk):
    from tradezone.core.portfolio import ClosedTrade
    coach = desk.bots["coach"]
    assert coach.risk_mult() == 1.0  # not enough history
    desk.portfolio.closed_trades = [ClosedTrade("AAPL", "long", 1, 100, 102 if i % 3 else 99, 2.0 if i % 3 else -1.0)
                                    for i in range(30)]
    assert coach.risk_mult() == 1.25
    desk.portfolio.closed_trades = [ClosedTrade("AAPL", "long", 1, 100, 99, -1.0 if i % 3 else 1.0)
                                    for i in range(30)]
    assert coach.risk_mult() == 0.6
    # even when sizing up, the position cap still binds
    desk.portfolio.closed_trades = [ClosedTrade("AAPL", "long", 1, 100, 102, 2.0) for _ in range(30)]
    desk.settings.risk.risk_per_trade = 0.5  # absurd risk: only the hard caps stop it
    prime(desk, "AAPL", 100.0)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    notional = desk.portfolio.position("AAPL").qty * 100.0
    assert 0 < notional <= desk.portfolio.equity * desk.settings.risk.max_position_pct * 1.05


def test_idle_with_healthy_results_lowers_bar_a_little(desk):
    coach = desk.bots["coach"]
    base = desk.settings.risk.min_signal_strength
    coach.last_entry_ts = now() - 7 * 3600
    for _ in range(10):  # keep adjusting: it must stop at the floor
        coach._last_adjust = 0
        run(coach.on_cycle())
    assert base - 0.05 - 1e-9 <= coach.bar() < base
    assert coach.bar() >= min(base, 0.30)
    assert desk.settings.risk.min_signal_strength == base  # the setting itself never changes


def test_learning_survives_a_restart(tmp_path, settings):
    path = str(tmp_path / "tz.db")
    d1 = TradingDesk(settings, journal=Journal(path))
    trade(d1, exit_=102.0)
    d1.bots["coach"].save()
    d1.journal.commit()
    d2 = TradingDesk(settings, journal=Journal(path))
    assert d2.bots["coach"].load()
    assert d2.bots["coach"].learned_trades == 1
    assert d2.bots["coach"].stats["symbol"]["AAPL"]["wins"] == 1


def test_learning_can_be_switched_off(desk):
    coach = desk.bots["coach"]
    for _ in range(30):
        coach._add("strategy", "momentum", 1.0, True, 10.0)
    coach.bench["AAPL"] = now() + 999
    desk.settings.coach_learning = False
    assert coach.strategy_mult("momentum") == 1.0 and coach.benched("AAPL") == 0 and coach.risk_mult() == 1.0


def test_bench_expiry_during_cycle_does_not_crash(desk):
    coach = desk.bots["coach"]
    for _ in range(6):
        coach.learn("AAPL", None, -10.0)
    coach.bench = {"AAPL": now() - 1, "TSLA": now() - 1, "NVDA": now() + 600}
    run(coach.on_cycle())  # benched() removes expired entries while counting
    assert set(coach.bench) == {"NVDA"} and coach.metrics["benched"] == 1
    assert coach.stats["symbol"]["AAPL"]["n"] == 3  # half the old record forgiven
    assert [b["symbol"] for b in coach.summary()["benched"]] == ["NVDA"]


def test_trade_open_across_restart_is_still_learned(tmp_path, settings):
    path = str(tmp_path / "tz.db")
    d1 = TradingDesk(settings, journal=Journal(path))
    prime(d1, "AAPL", 100.0)
    d1.bots["quant"].scores["AAPL"] = {"momentum": 0.9, "mean_reversion": 0.0, "breakout": 0.0,
                                       "ensemble": 0.6, "regime": "trending_up"}
    d1.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(d1.bus.drain())
    d1.bots["coach"].save()
    d2 = TradingDesk(settings, journal=Journal(path))
    assert d2.bots["coach"].load() and "paper|AAPL" in d2.bots["coach"].open
    coach = d2.bots["coach"]
    d2.bus.publish("trade.log", {"symbol": "AAPL", "action": "close", "pnl": 25.0, "position_after": 0.0,
                                 "mode": "paper"})
    run(d2.bus.drain())
    assert coach.learned_trades == 1 and coach.stats["strategy"]["momentum"]["wins"] == 1


def test_paper_and_live_trades_are_tracked_separately(desk):
    coach = desk.bots["coach"]
    pub = lambda **t: (desk.bus.publish("trade.log", {"symbol": "BTC-USD", "pnl": None, "position_after": 1.0, **t}),
                       run(desk.bus.drain()))
    pub(action="open", mode="paper")
    pub(action="open", mode="live")
    pub(action="close", mode="live", pnl=-5.0, position_after=0.0)
    assert "paper|BTC-USD" in coach.open and "live|BTC-USD" not in coach.open
    assert coach.stats["symbol"]["BTC-USD"]["n"] == 1
