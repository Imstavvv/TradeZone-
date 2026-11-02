"""Synthetic market with regime switching, for paper trading and demos."""

from __future__ import annotations

import math
import random

from ..core.models import Tick, now
from .base import MarketFeed

DEFAULT_PRICES = {
    "BTC-USD": 68000.0,
    "ETH-USD": 3500.0,
    "SOL-USD": 160.0,
    "XRP-USD": 2.5,
    "XLM-USD": 0.4,
    "ATOM-USD": 6.0,
    "HBAR-USD": 0.2,
    "AAPL": 225.0,
    "NVDA": 135.0,
    "TSLA": 250.0,
    "SPY": 560.0,
    "MSFT": 420.0,
}


class _SymbolState:
    def __init__(self, price: float, vol: float) -> None:
        self.price = price
        self.base_vol = vol
        self.drift = 0.0
        self.vol_mult = 1.0
        self.regime_left = 0


class SimulatedFeed(MarketFeed):
    name = "sim"

    def __init__(self, symbols: list[str], seed: int = 7, step_vol: float = 0.0025) -> None:
        self.rng = random.Random(seed)
        self.clock = now()
        self.state = {
            s: _SymbolState(
                DEFAULT_PRICES.get(s, self.rng.uniform(20, 500)),
                step_vol * (1.6 if s.endswith("-USD") else 1.0),
            )
            for s in symbols
        }

    def _maybe_switch_regime(self, st: _SymbolState) -> None:
        if st.regime_left > 0:
            st.regime_left -= 1
            return
        r = self.rng.random()
        if r < 0.45:  # trend
            st.drift = self.rng.choice([-1, 1]) * st.base_vol * self.rng.uniform(0.15, 0.4)
            st.vol_mult = 0.8
        elif r < 0.85:  # range / mean reversion
            st.drift = 0.0
            st.vol_mult = 1.0
        else:  # volatility burst
            st.drift = 0.0
            st.vol_mult = 2.5
        st.regime_left = self.rng.randint(40, 160)

    async def poll(self) -> list[Tick]:
        return self.step()

    def step(self, dt: float = 1.0) -> list[Tick]:
        self.clock += dt
        ticks = []
        for sym, st in self.state.items():
            self._maybe_switch_regime(st)
            sigma = st.base_vol * st.vol_mult
            shock = self.rng.gauss(0, 1)
            if self.rng.random() < 0.003:  # rare jump
                shock += self.rng.choice([-1, 1]) * self.rng.uniform(4, 8)
            st.price *= math.exp(st.drift - 0.5 * sigma**2 + sigma * shock)
            vol = abs(self.rng.gauss(1000, 300))
            ticks.append(Tick(sym, round(st.price, 4), round(vol, 2), self.clock))
        return ticks
