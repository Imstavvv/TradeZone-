"""Regression tests for the real-money review fixes (Uphold + Kraken)."""

import httpx
import pytest

from tests.helpers import run
from tests.test_kraken import SECRET, FakeKraken, kraken_desk
from tradezone.config import Settings
from tradezone.desk import ModeSwitchError, TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def test_practice_fee_goes_back_to_normal_after_leaving_uphold(monkeypatch):
    monkeypatch.delenv("TZ_CRYPTO_FEE_BPS", raising=False)
    s = Settings(feed="coinbase", uphold_token="t" * 30)
    s.validate()
    assert s.crypto_fee_bps == 100
    s.uphold_token, s.live_venue = "", "kraken"
    s.apply_venue_costs()
    assert s.crypto_fee_bps == 40  # Kraken practice is charged Kraken's 0.40% fee, not Uphold's spread
    s.live_venue = "alpaca"
    s.apply_venue_costs()
    assert s.crypto_fee_bps == 25  # back to Alpaca's normal fee


def test_unlinking_resets_venue_and_fee(monkeypatch):
    monkeypatch.delenv("TZ_CRYPTO_FEE_BPS", raising=False)
    desk = TradingDesk(Settings(feed="coinbase", uphold_token="t" * 30), journal=Journal(":memory:"),
                       feed=SimulatedFeed(["BTC-USD"]))
    assert desk.settings.crypto_fee_bps == 100
    run(desk.unlink_broker())
    assert desk.settings.live_venue == "auto" and desk.settings.crypto_fee_bps == 25


def test_server_configured_kraken_remembers_your_coins_after_restart():
    fk = FakeKraken(held={"XXBT": 0.5})
    desk = kraken_desk(fk)
    desk._save_account_row("kraken_baseline", {"XXBT": 0.5})
    assert desk.load_linked_broker() is False  # keys come from the server, not the app...
    assert desk.live_broker.baseline == {"XXBT": 0.5}  # ...but the owner's BTC is still theirs
    assert run(desk.live_broker.positions()) == {}


def test_a_kraken_outage_while_going_live_is_a_clear_switch_error():
    fk = FakeKraken()
    desk = kraken_desk(fk)
    real = fk.handler

    def flaky(req):
        if req.url.path.endswith("/Balance") and fk.calls.count("TradeBalance") >= 1:
            raise httpx.ConnectError("down")
        return real(req)
    fk.handler = flaky
    desk.live_broker.client._transport = httpx.MockTransport(flaky)
    with pytest.raises(ModeSwitchError, match="couldn't read your Kraken balances"):
        run(desk.switch_mode("live"))
    assert not desk.is_live


def test_kraken_link_closes_its_client_when_kraken_is_unreachable(monkeypatch, tmp_path):
    from tradezone.brokers import kraken as kmod

    closed = []

    def down(req):
        raise httpx.ConnectError("down")

    class Probe(kmod.KrakenBroker):
        def __init__(self, key, secret, client=None):
            super().__init__(key, secret, client=httpx.AsyncClient(
                base_url="https://api.kraken.com", transport=httpx.MockTransport(down)))

        async def close(self):
            closed.append(True)
            await super().close()
    monkeypatch.setattr(kmod, "KrakenBroker", Probe)
    desk = TradingDesk(Settings(feed="coinbase"), journal=Journal(str(tmp_path / "tz.db")),
                       feed=SimulatedFeed(["BTC-USD"]))
    with pytest.raises(ValueError, match="Couldn't reach Kraken"):
        run(desk.link_broker("KEY", SECRET, venue="kraken"))
    assert closed and desk.live_broker is None


def test_startup_loads_what_the_venue_can_trade():
    fk = FakeKraken()
    desk = kraken_desk(fk)
    assert desk.live_broker.supports("BTC-USD") is None
    run(desk.live_broker.prepare())
    assert desk.live_broker.supports("BTC-USD") and desk.live_broker.supports("AAPL") is False
