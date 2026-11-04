"""Shared test helpers (one copy, imported by every test module)."""

import asyncio

from tradezone.brokers.paper import PaperBroker
from tradezone.core.models import Snapshot, Tick


def run(coro):
    return asyncio.run(coro)


def prime(desk, symbol="AAPL", price=100.0, atr=0.5):
    """Give the desk a live price and indicator snapshot for ``symbol`` without running the feed."""
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


class FakeLiveBroker(PaperBroker):
    """Stands in for the Alpaca live account."""

    name = "alpaca"
    label = "Alpaca"
    is_live = True

    def supports(self, symbol):
        from tradezone.config import Settings
        return Settings.live_tradeable(symbol)

    def __init__(self, cash=2_500.0, positions=None):
        super().__init__(0, 0)
        self.cash, self._positions, self.submitted = cash, positions or {}, []

    async def submit(self, order):
        self.submitted.append(order)
        return await super().submit(order)

    async def account(self):
        return {"cash": self.cash, "equity": self.cash}

    async def positions(self):
        return dict(self._positions)


ORIGIN = "https://testserver"




