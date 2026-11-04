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
