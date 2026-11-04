from __future__ import annotations

import abc

from ..core.models import Fill, Order, Tick


class Broker(abc.ABC):
    """Order routing interface used by the Execution Trader bot."""

    name = "broker"
    label = "broker"  # how the dashboard names it
    is_live = False

    def supports(self, symbol: str) -> bool | None:
        """Whether this venue can trade ``symbol`` (None = unknown, let the order decide)."""
        return None

    async def prepare(self) -> None:
        """Load what this venue can trade (called at start-up, so ``supports`` knows)."""

    async def refresh_market(self) -> None:
        """Refresh what's needed for ``sell_check`` (called every half minute)."""

    def min_qty(self, symbol: str, price: float) -> float:
        """The smallest amount this venue lets you sell (0 = no known minimum)."""
        return 0.0

    async def fresh_quote(self, symbol: str) -> dict | None:
        """A fresh {bid, ask, ...} for ``symbol`` just before an order, if the venue offers one."""
        return None

    def can_short(self, symbol: str) -> bool:
        """Whether this account can sell ``symbol`` short right now (borrow it and buy it back later)."""
        return False

    def sell_check(self, symbol: str, qty: float, price: float, strict: bool = True) -> str | None:
        """Why buying could leave coins that can't be sold again, or None when it's fine."""
        return None

    def on_prices(self, ticks: list[Tick]) -> None:
        """Receive the latest prices (used by simulated brokers)."""

    @abc.abstractmethod
    async def submit(self, order: Order) -> list[Fill]:
        """Send an order. Mutates ``order`` status and returns any immediate fills."""

    async def poll(self, order: Order) -> list[Fill]:
        """Return new fills for a working order (for asynchronous brokers)."""
        return []

    async def cancel(self, order: Order) -> None:
        return None

    async def account(self) -> dict | None:
        """Broker-side account snapshot used for reconciliation, if available."""
        return None

    async def position_qty(self, symbol: str) -> float | None:
        """The broker's actual quantity for one symbol (None if this broker doesn't track positions)."""
        pos = await self.positions()
        if pos is None:
            return None
        from ..core.util import norm_symbol
        return next((q for s, q in pos.items() if norm_symbol(s) == norm_symbol(symbol)), 0.0)

    async def positions(self) -> dict[str, float] | None:
        """Broker-side positions (symbol -> signed qty) for reconciliation, if available."""
        return None

    async def close(self) -> None:
        return None
