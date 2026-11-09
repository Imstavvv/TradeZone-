"""The 15 helper bots: ten advisors, the Pullback Hunter, and four operations bots."""

import time

import pytest

from tests.helpers import capture, prime, run
from tradezone.config import Settings
from tradezone.core.models import Fill, Order, OrderStatus, Side, Signal, Snapshot
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

SYMS = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD"]


def make(**kw):
    s = Settings(feed="sim", symbols=list(SYMS), wide_stop=False, accumulate=False, **kw)
    s.risk.symbol_cooldown_s = 0
    s.risk.max_orders_per_minute = 10**6
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))


def ind(price, **kw):
    d = {"atr": price * 0.004, "volatility": 0.002, "rsi": 55, "ema_fast": price, "ema_slow": price, "sma50": price,
         "donchian_high": price * 1.05, "donchian_low": price * 0.95, "swing_high": price * 1.06,
         "swing_low": price * 0.97, "volume_ratio": 1.0}
    d.update(kw)
    return d


def snap(desk, sym, price, ts=None, **kw):
    s = Snapshot(sym, price, ind(price, **kw), ts or time.time())
    desk.bots["risk"].snapshots[sym] = s
    desk.bus.publish("market.snapshot", s)
    run(desk.bus.drain())
    return s


def buy(desk, sym, price, strength=0.9):
    prime(desk, sym, price, atr=price * 0.005)
    rejected = capture(desk, "signal.rejected")
    desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, strength, "t", "quant"))
    run(desk.bus.drain())
    return desk.portfolio.position(sym), rejected


def advice(desk, name, sym="ETH-USD", side=Side.BUY):
    return desk.bots[name].advise(Signal(sym, side, 0.6, "t", "quant"), desk.bots["risk"].snapshots.get(sym))


def test_the_team_has_42_bots_and_13_advisors():
    desk = make()
    assert len(desk.bots) == 42 and len(desk.advisors) == 15  # 13 advisors + the Slippage Tracker and Exchange Health
    assert {"weather", "pullback", "fees", "accountant"} <= set(desk.bots)


def test_market_weather_ignores_normal_swings_in_a_jumpy_market():
    desk = make()
    for i in range(16):
        snap(desk, "BTC-USD", 60_000 * (1 - 0.0015 * i), volatility=0.004)  # -2.1% is ordinary here
    assert advice(desk, "weather")[0] is None


def test_market_weather_holds_alt_buys_while_bitcoin_falls():
    desk = make(falling_buys="block")  # (the block itself; "areas" lets a buy area through - see test_falling_buys)
    for i in range(16):
        snap(desk, "BTC-USD", 60_000 * (1 - 0.0015 * i), volatility=0.0005)  # a calm market: -2.1% is a storm
    veto, _, _ = advice(desk, "weather")
    assert veto and "Bitcoin is falling" in veto
    pos, rej = buy(desk, "ETH-USD", 3_000.0)
    assert pos.is_flat and "Market Weather" in rej[-1]["reason"]
    assert advice(desk, "weather", "BTC-USD")[0] is None  # Bitcoin itself isn't held back by itself


@pytest.mark.with_killed  # (tests a feature switched off after failing on unseen data)
def test_volatility_sizer_trims_wild_coins():
    desk = make()
    for _ in range(40):
        snap(desk, "SOL-USD", 150.0, volatility=0.002)
    snap(desk, "SOL-USD", 150.0, volatility=0.006)  # 3x the usual
    assert advice(desk, "volsizer", "SOL-USD")[2] == pytest.approx(0.6)


@pytest.mark.with_killed  # (tests a feature switched off after failing on unseen data)
def test_momentum_ranker_favours_the_leaders():
    desk = make()
    for i in range(21):
        for sym, drift in (("SOL-USD", 0.002), ("ETH-USD", 0.0), ("XRP-USD", -0.002)):
            snap(desk, sym, 100 * (1 + drift) ** i)
    assert advice(desk, "ranker", "SOL-USD")[1] > 0 and advice(desk, "ranker", "XRP-USD")[1] < 0


def test_overextension_guard_wont_chase():
    desk = make()
    snap(desk, "ETH-USD", 3_000.0, ema_slow=2_880.0, atr=12.0)  # 10 ATR above its average
    assert "stretched" in advice(desk, "stretch")[0]


def test_resistance_mapper_trims_buys_right_under_a_ceiling():
    desk = make()
    snap(desk, "ETH-USD", 3_000.0, swing_high=3_030.0, donchian_high=3_030.0)  # +1% to the ceiling, target +6%
    assert advice(desk, "levels")[2] == pytest.approx(0.8)


def test_correlation_officer_blocks_a_third_copy_of_the_same_bet():
    desk = make()
    for i in range(40):
        for sym in ("SOL-USD", "ETH-USD", "XRP-USD"):
            snap(desk, sym, 100 * (1.003 if i % 2 else 0.998) ** (i // 2 + 1))
    for sym in ("ETH-USD", "XRP-USD"):
        p = desk.portfolio.position(sym)
        p.qty, p.avg_price, p.last_price = 1.0, 100.0, 100.0
    veto, _, _ = advice(desk, "correlation", "SOL-USD")
    assert veto and "moves almost exactly like" in veto


def test_whale_watcher_spots_accumulation():
    desk = make()
    snap(desk, "DOGE-USD", 0.2)
    snap(desk, "DOGE-USD", 0.2002, volume_ratio=3.5)  # huge volume, flat price
    assert advice(desk, "whales", "DOGE-USD")[1] == pytest.approx(0.05)


def test_liquidity_guard_waits_out_a_dead_market():
    desk = make()
    snap(desk, "ETH-USD", 3_000.0, volume_ratio=0.1)
    assert "dried up" in advice(desk, "liquidity")[0]


def test_flash_crash_guard_pauses_new_trades_after_a_market_wide_shock():
    desk = make()
    t0 = 1_800_000_000
    for i in range(40):
        for sym in ("ETH-USD", "SOL-USD", "XRP-USD"):
            snap(desk, sym, 100 * (1.001 if i % 2 else 0.999), ts=t0 + 60 * i)
    for sym in ("ETH-USD", "SOL-USD", "XRP-USD"):
        snap(desk, sym, 85.0, ts=t0 + 60 * 41)  # all three crash together
    assert "market-wide shock" in advice(desk, "shockguard")[0]


def test_big_picture_trend_trims_buys_against_the_15_minute_trend():
    desk = make()
    for i in range(15 * 25):
        snap(desk, "ETH-USD", 3_000 * (1 - 0.0003 * i))  # a steady fall
    assert advice(desk, "htf")[2] == pytest.approx(0.8) and advice(desk, "htf", side=Side.SELL)[1] > 0


def test_pullback_hunter_buys_the_retest_of_a_breakout():
    desk = make(pullback=True)  # (off by default now: it hurt profit on replays)
    desk.set_bot_enabled("quant", False)
    desk.set_bot_enabled("skimmer", False)
    ideas = capture(desk, "signal.proposed")
    prime(desk, "SOL-USD", 150.0, atr=0.6)
    snap(desk, "SOL-USD", 153.5, donchian_high=153.0)  # breaks out above 153
    snap(desk, "SOL-USD", 153.2, donchian_high=153.5, ema_fast=152.0, ema_slow=151.0)  # dips back to 153.0-153.46
    assert any(i.source == "pullback" for i in ideas) and desk.bots["pullback"].metrics["retests"] == 1


def test_fee_auditor_plans_with_the_fee_really_paid():
    desk = make()
    desk.mode = "live"  # (the audit only learns from real-money fills)
    for i in range(12):
        desk.bus.publish("order.filled", Fill(f"o{i}", "BTC-USD", Side.BUY, 0.01, 50_000.0, 0.01 * 50_000 * 0.0016))
    run(desk.bus.drain())
    assert desk.settings.crypto_fee_bps == pytest.approx(16.0, abs=0.5)
    desk.mode = "paper"


def test_slippage_tracker_trims_coins_that_fill_badly():
    desk = make()
    exe = desk.bots["execution"]
    for i in range(6):
        exe.ref_price[f"o{i}"] = 100.0
        desk.bus.publish("order.filled", Fill(f"o{i}", "SOL-USD", Side.BUY, 1, 100.5, 0.0))  # 0.5% worse each time
    run(desk.bus.drain())
    assert advice(desk, "slippage", "SOL-USD")[2] == pytest.approx(0.6)


def test_exchange_health_waits_while_the_exchange_keeps_rejecting():
    desk = make()
    desk.mode = "live"
    for i in range(5):
        o = Order("SOL-USD", Side.BUY, 1, f"r{i}", status=OrderStatus.REJECTED, reason="kraken: EService:Unavailable")
        desk.bus.publish("order.update", o)
    run(desk.bus.drain())
    assert "keeps rejecting" in advice(desk, "uptime")[0]
    desk.mode = "paper"


def test_accountant_reports_each_day():
    desk = make()
    a = desk.bots["accountant"]
    y = time.time() - 86_400
    for pnl, sym in ((12.5, "SOL-USD"), (-4.0, "XRP-USD"), (3.0, "DOGE-USD")):
        a.on_trade(type("E", (), {"payload": {"symbol": sym, "pnl": pnl, "fee": 0.5, "ts": y}})())
    alerts = capture(desk, "alert")
    run(a.on_cycle())
    run(desk.bus.drain())
    msg = alerts[-1].message
    assert "Daily report" in msg and "+11.50 after fees" in msg and "2/3 won" in msg and "best SOL-USD" in msg
    assert desk.state()["daily_reports"][0]["closed"] == 3


def test_advisors_can_be_switched_off_one_by_one():
    desk = make()
    for i in range(16):
        snap(desk, "BTC-USD", 60_000 * (1 - 0.0015 * i), volatility=0.0005)
    desk.set_bot_enabled("weather", False)
    assert buy(desk, "ETH-USD", 3_000.0)[0].qty > 0


def test_accountant_and_fee_auditor_remember_across_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    s = Settings(feed="sim", symbols=list(SYMS))
    d1 = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))
    a = d1.bots["accountant"]
    a.on_trade(type("E", (), {"payload": {"symbol": "SOL-USD", "pnl": 7.0, "fee": 0.2, "ts": time.time()}})())
    d1.bots["fees"].paid.extend([16.0] * 12)
    run(d1.shutdown())
    s2 = Settings(feed="sim", symbols=list(SYMS))
    d2 = TradingDesk(s2, feed=SimulatedFeed(s2.symbols), journal=Journal(db))
    run(d2.start())
    today = d2.bots["accountant"].summary()[0]
    assert today["pnl"] == pytest.approx(7.0) and today["best"] == (7.0, "SOL-USD")
    assert d2.settings.crypto_fee_bps == pytest.approx(16.0)  # plans with the fee really paid, not the guess
    run(d2.shutdown())


def test_the_liquidity_guard_isnt_fooled_by_one_empty_minute_on_a_thin_market():
    from tradezone.bots.advisors import LiquidityBot
    from tradezone.config import Settings
    from tradezone.core.models import Side, Signal, Snapshot
    from tradezone.desk import TradingDesk
    from tradezone.journal import Journal
    d = TradingDesk(Settings(feed="sim", symbols=["DOGE-USD"]), journal=Journal(":memory:"))
    g = next(a for a in d.advisors if isinstance(a, LiquidityBot))
    sig = Signal("DOGE-USD", Side.BUY, 0.9, "t", "quant")
    quiet_minute = Snapshot("DOGE-USD", 0.2, {"volume_ratio": 0.0, "volume_ratio_5": 0.9})  # busy minutes just before
    assert g.advise(sig, quiet_minute)[0] is None
    dried_up = Snapshot("DOGE-USD", 0.2, {"volume_ratio": 0.0, "volume_ratio_5": 0.1})  # five quiet minutes in a row
    assert "dried up" in g.advise(sig, dried_up)[0]
    older = Snapshot("DOGE-USD", 0.2, {"volume_ratio": 0.1})  # (no 5-bar figure: as before)
    assert "dried up" in g.advise(sig, older)[0]
