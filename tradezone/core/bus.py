"""Asynchronous publish/subscribe event bus connecting the bot team.

Events are queued and dispatched in FIFO order by ``drain()``. This keeps
handler execution deterministic (great for simulation and tests) and avoids
unbounded recursion when handlers publish further events.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .models import now

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None] | None]
ErrorHook = Callable[[str, "Event", BaseException], None]


@dataclass
class Event:
    topic: str
    payload: Any
    source: str = "system"
    ts: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "source": self.source, "ts": self.ts, "payload": jsonable(self.payload)}


def jsonable(x: Any) -> Any:
    """Convert models nested anywhere in dicts/lists (e.g. a Signal inside a review) to plain data."""
    if hasattr(x, "to_dict"):
        return x.to_dict()
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


@dataclass
class _Sub:
    topic: str
    handler: Handler
    owner: str


class EventBus:
    def __init__(self, max_events_per_drain: int = 100_000) -> None:
        self._subs: list[_Sub] = []
        self._queue: deque[Event] = deque()
        self._lock = asyncio.Lock()
        self._max = max_events_per_drain
        self.on_error: ErrorHook | None = None

    def subscribe(self, topic: str, handler: Handler, owner: str = "anon") -> None:
        """Subscribe to an exact topic, a prefix wildcard (``order.*``) or ``*``."""
        self._subs.append(_Sub(topic, handler, owner))

    def publish(self, topic: str, payload: Any = None, source: str = "system") -> Event:
        ev = Event(topic, payload, source)
        self._queue.append(ev)
        return ev

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        if pattern == "*" or pattern == topic:
            return True
        return pattern.endswith(".*") and topic.startswith(pattern[:-1])

    async def drain(self) -> int:
        """Dispatch queued events (including ones published while draining)."""
        async with self._lock:
            n = 0
            while self._queue:
                if n >= self._max:
                    log.error("event storm: dropping %d queued events", len(self._queue))
                    self._queue.clear()
                    break
                ev = self._queue.popleft()
                n += 1
                for sub in list(self._subs):
                    if not self._matches(sub.topic, ev.topic):
                        continue
                    try:
                        res = sub.handler(ev)
                        if inspect.isawaitable(res):
                            await res
                    except Exception as exc:  # isolate faulty bots
                        log.exception("handler %s failed on %s", sub.owner, ev.topic)
                        if self.on_error:
                            self.on_error(sub.owner, ev, exc)
            return n
