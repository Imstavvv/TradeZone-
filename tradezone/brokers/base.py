from __future__ import annotations

import abc

from ..core.models import Fill, Order, Tick


class Broker(abc.ABC):
    """Order routing interface used by the Execution Trader bot."""

    name = "broker"
    is_live = False

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

    async def close(self) -> None:
        return None
