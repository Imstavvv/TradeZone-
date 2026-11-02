import asyncio

import pytest

from tradezone.core.models import Side, Signal, SignalKind, Snapshot, Tick


def run(coro):
    return asyncio.run(coro)


def prime(desk, symbol="AAPL", price=100.0, atr=0.5):
    desk.prices[symbol] = price
    desk.broker.on_prices([Tick(symbol, price)])
    desk.portfolio.mark(symbol, price)
    ind = {"atr": atr, "volatility": 0.003, "rsi": 50, "ema_fast": price, "ema_slow": price,
           "sma50": price, "donchian_high": price, "donchian_low": price}
    desk.bots["risk"].snapshots[symbol] = Snapshot(symbol, price, ind)


def capture(desk, topic):
    out = []
    desk.bus.subscribe(topic, lambda e: out.append(e.payload), owner="test")
    return out


def test_team_has_at_least_six_distinct_roles(desk):
    roles = {b.role for b in desk.bots.values()}
    assert len(desk.bots) >= 6 and len(roles) == len(desk.bots)


def test_signal_flows_through_risk_execution_portfolio(desk):
    prime(desk)
    fills = capture(desk, "order.filled")
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.8, "test", "quant"))
    run(desk.bus.drain())
    assert len(fills) == 1
    pos = desk.portfolio.position("AAPL")
    assert pos.qty > 0
    # sized by volatility: risk budget / stop distance, capped at max_position_pct
    assert pos.qty * 100 <= desk.portfolio.equity * desk.settings.risk.max_position_pct + 1
    assert pos.stop_price < 100 < pos.take_profit


def test_risk_blocks_entries_when_halted_but_allows_exits(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").qty > 0
    rejected = capture(desk, "signal.rejected")
    prime(desk, "NVDA", 50)
    desk.halt("test", flatten=False)
    desk.bus.publish("signal.proposed", Signal("NVDA", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert rejected and "kill switch" in rejected[-1]["reason"]
    assert desk.portfolio.position("NVDA").is_flat
    desk.flatten("test")
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda d: setattr(d, "paused", True), "paused"),
        (lambda d: setattr(d.settings.risk, "min_signal_strength", 0.95), "conviction"),
        (lambda d: setattr(d.settings.risk, "max_open_positions", 0), "max open"),
    ],
)
def test_risk_rejections(desk, mutate, reason):
    prime(desk)
    mutate(desk)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.8, "t", "quant"))
    run(desk.bus.drain())
    assert rejected and reason in rejected[0]["reason"]


def test_short_disabled_by_default(desk):
    prime(desk)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert "short" in rejected[0]["reason"]


def test_stop_loss_triggers_exit(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    stop = desk.portfolio.position("AAPL").stop_price
    trades = capture(desk, "trade.closed")
    crash = stop * 0.99
    desk.prices["AAPL"] = crash
    desk.broker.on_prices([Tick("AAPL", crash)])
    desk.bus.publish("market.tick", Tick("AAPL", crash))
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat
    assert trades and trades[0].pnl < 0


def test_trailing_stop_ratchets_up(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("AAPL")
    s0 = pos.stop_price
    desk.bus.publish("market.tick", Tick("AAPL", 101.0))
    run(desk.bus.drain())
    assert pos.stop_price > s0


def test_kill_switch_trips_on_drawdown(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    desk.portfolio.cash -= desk.portfolio.equity * 0.2  # simulate a large loss
    run(desk.bots["risk"].on_cycle())
    run(desk.bus.drain())
    assert desk.halted
    assert desk.portfolio.position("AAPL").is_flat  # flattened on trip
    desk.reset_halt()
    assert not desk.halted


def test_execution_drops_reduce_only_without_position(desk):
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.SELL, 1, "x", "portfolio", SignalKind.EXIT))
    run(desk.bus.drain())
    assert not desk.bots["execution"].orders


def test_disabled_bot_ignores_events(desk):
    prime(desk)
    desk.set_bot_enabled("risk", False)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("AAPL").is_flat  # no risk manager -> no orders


def test_full_simulation_trades_without_errors(desk):
    async def go():
        await desk.start()
        for _ in range(1500):
            await desk.step()

    run(go())
    assert all(b.errors == 0 for b in desk.bots.values())
    assert desk.portfolio.closed_trades
    assert desk.bots["sentinel"].metrics["journaled"] > 0
    assert desk.journal.query("order.filled", 5)
    # ledger integrity: equity == cash + marked positions
    p = desk.portfolio
    assert p.equity == pytest.approx(p.cash + sum(x.qty * x.last_price for x in p.positions.values()))
