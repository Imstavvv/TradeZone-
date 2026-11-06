import pytest

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal


@pytest.fixture
def settings():
    s = Settings(never_sell_at_loss=False, feed="sim", wide_stop=False, accumulate=False,
                 loser_cap=0.0)  # most tests check the normal plan (own tests cover these)
    s.risk.symbol_cooldown_s = 0
    s.risk.max_orders_per_minute = 10**6
    return s


@pytest.fixture
def desk(settings):
    return TradingDesk(settings, journal=Journal(":memory:"))


@pytest.fixture(autouse=True)
def _no_history_pause(monkeypatch):
    """The Trend Scout spaces out history requests (the exchange's rate limit); tests don't need to wait."""
    import tradezone.bots.trends as trends
    waited = []

    async def fake(s):
        waited.append(s)
    monkeypatch.setattr(trends, "_pause", fake)
    return waited
