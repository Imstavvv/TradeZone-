import pytest

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal


@pytest.fixture
def settings():
    s = Settings()
    s.risk.symbol_cooldown_s = 0
    s.risk.max_orders_per_minute = 10**6
    return s


@pytest.fixture
def desk(settings):
    return TradingDesk(settings, journal=Journal(":memory:"))
