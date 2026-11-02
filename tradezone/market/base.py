from __future__ import annotations

import abc

from ..core.models import Tick


class MarketFeed(abc.ABC):
    """Source of live prices. ``poll`` is called once per desk cycle."""

    name = "feed"

    @abc.abstractmethod
    async def poll(self) -> list[Tick]: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None
