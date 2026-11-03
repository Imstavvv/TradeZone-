import asyncio

import pytest
from fastapi.testclient import TestClient

from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import ModeSwitchError, TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.server import create_app
from tests.helpers import FakeLiveBroker, prime, run


def make_desk(live_broker=None, **kw):
    kw.setdefault("symbols", ["BTC-USD", "ETH-USD", "AAPL", "NVDA"])
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", **kw)
    s.risk.symbol_cooldown_s = 0
    feed = SimulatedFeed(s.symbols)
    return TradingDesk(s, feed=feed, journal=Journal(":memory:"), live_broker=live_broker or FakeLiveBroker())


def test_desk_starts_in_paper_even_when_live_is_configured():
    desk = make_desk()
    run(desk.start())
    assert desk.mode == "paper" and not desk.is_live
    assert desk.broker is desk.paper_broker
    assert desk.state()["live"]["available"] is True


def test_switch_to_live_and_back():
    live = FakeLiveBroker(cash=2_500)
    desk = make_desk(live)
    prime(desk)
    paper_cash = desk.paper_portfolio.cash
    res = run(desk.switch_mode("live"))
    assert res["changed"] and desk.is_live and desk.broker is live
    assert desk.portfolio.cash == 2_500 and desk.portfolio is not desk.paper_portfolio
    # orders now go to the live broker
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert live.submitted and desk.portfolio.position("AAPL").qty > 0
    # can't leave live while holding positions
    with pytest.raises(ModeSwitchError, match="open live position"):
        run(desk.switch_mode("paper"))
    desk.flatten("test")
    run(desk.bus.drain())
    run(desk.switch_mode("paper"))
    assert desk.mode == "paper" and desk.portfolio.cash == paper_cash
    assert desk.state()["mode"] == "paper"


def test_one_click_switch_back_flattens_first():
    desk = make_desk()
    prime(desk)
    run(desk.switch_mode("live"))
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.open_positions
    run(desk.switch_mode("paper", flatten=True))
    assert desk.mode == "paper" and not desk.paused
    assert desk.live_portfolio.position("AAPL").is_flat


def test_live_capital_cap():
    desk = make_desk(FakeLiveBroker(cash=50_000), live_max_capital=500)
    prime(desk)
    run(desk.switch_mode("live"))
    assert desk.portfolio.cash == 500


@pytest.mark.parametrize(
    "setup,reason",
    [
        (lambda d: d.halt("x", flatten=False), "kill switch"),
        (lambda d: setattr(d.live_broker, "_positions", {"TSLA": 3}), "already holds"),
        (lambda d: setattr(d.live_broker, "cash", 0), "no cash"),
    ],
)
def test_switch_blockers(setup, reason):
    desk = make_desk()
    setup(desk)
    with pytest.raises(ModeSwitchError, match=reason):
        run(desk.switch_mode("live"))
    assert desk.mode == "paper"


def test_open_paper_positions_block_going_live():
    desk = make_desk()
    prime(desk)
    desk.bus.publish("signal.proposed", Signal("AAPL", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    with pytest.raises(ModeSwitchError, match="open paper position"):
        run(desk.switch_mode("live"))


def test_unconfigured_live_is_unavailable(desk):
    assert desk.live_broker is None
    with pytest.raises(ModeSwitchError, match="LIVE_KEY"):
        run(desk.switch_mode("live"))


def test_exposed_dashboard_needs_token():
    desk = make_desk(host="0.0.0.0")
    with pytest.raises(ModeSwitchError, match="PIN"):
        run(desk.switch_mode("live"))


def test_mode_api_requires_typed_confirmation():
    desk = make_desk()
    with TestClient(create_app(desk, run_desk=False)) as c:
        chk = c.get("/api/mode").json()
        assert chk["target"] == "live" and chk["blockers"] == [] and chk["account"]["cash"] == 2_500
        assert c.post("/api/mode", json={"mode": "live"}).status_code == 400
        assert c.post("/api/mode", json={"mode": "live", "confirm": "live"}).json()["mode"] == "live"
        assert c.get("/api/state").json()["mode"] == "live"
        assert c.post("/api/mode", json={"mode": "paper"}).json()["mode"] == "paper"


def test_live_withdrawals_are_manual_and_deposits_not_credited():
    desk = make_desk(api_token="t", withdrawal_cooldown_h=0)
    run(desk.switch_mode("live"))
    eth = "0x52908400098527886E0F7030069857D2E4169EE7"
    w = desk.wallets.link("ETH", eth)
    cash = desk.portfolio.cash
    assert desk.wallets.record_deposit("ETH", "USDC", 100, "tx", from_address=eth).note.startswith("live mode")
    assert desk.wallets.withdraw(w.id, "USDC", 50).status == "manual_required"
    assert desk.portfolio.cash == cash


def test_books_stay_balanced_across_switches():
    desk = make_desk()

    async def go():
        await desk.start()
        for _ in range(200):
            await desk.step()
        desk.flatten("x")
        await desk.bus.drain()
        await desk.switch_mode("live")
        for _ in range(200):
            await desk.step()

    asyncio.run(go())
    assert desk.bots["sentinel"].books_balanced and desk.bots["execution"].accuracy == 1.0
