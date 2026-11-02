from __future__ import annotations

import abc

from ..core.models import Tick


class MarketFeed(abc.ABC):
    """Source of live prices. ``poll`` is called once per desk cycle."""

    name = "feed"
    last_error = ""  # most recent problem talking to the data source, for the dashboard

    @abc.abstractmethod
    async def poll(self) -> list[Tick]: ...

    async def start(self) -> None:
        """Open streaming connections, if any (called once when the desk starts)."""

    async def history(self, symbol: str, granularity: int, limit: int = 300) -> list[dict] | None:
        """Recent completed candles (oldest first) to pre-warm indicators; None if unsupported."""
        return None

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None
