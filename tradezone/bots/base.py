"""Base class for every member of the bot team."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..core.bus import Event
from ..core.models import now

if TYPE_CHECKING:
    from ..desk import TradingDesk


@dataclass
class BotStatus:
    name: str
    role: str
    title: str
    description: str
    enabled: bool
    state: str
    last_action: str
    last_action_ts: float | None
    errors: int
    metrics: dict[str, Any]
    log: list[dict[str, Any]] = field(default_factory=list)
    actions_per_min: float = 0.0
    total_actions: int = 0
    mission: str = ""


class Bot:
    #: short machine name, e.g. "scout"
    name = "bot"
    #: human readable callsign
    title = "Bot"
    #: one-line role used across the UI
    role = "generic"
    description = ""
    #: how this bot serves the team rules (make money, take trades, grow, learn the markets)
    mission = ""
    #: if True the bot can't be disabled from the UI (safety-critical)
    critical = False

    def __init__(self, desk: "TradingDesk") -> None:
        self.desk = desk
        self.enabled = True
        self.errors = 0
        self.last_action = "standing by"
        self.last_action_ts: float | None = None
        self.metrics: dict[str, Any] = {}
        self._log: deque[dict[str, Any]] = deque(maxlen=200)
        self._action_times: deque[float] = deque(maxlen=600)
        self.total_actions = 0

    # -- wiring ---------------------------------------------------------
    def subscriptions(self) -> dict[str, Callable[[Event], Any]]:
        return {}

    def attach(self) -> None:
        for topic, handler in self.subscriptions().items():
            self.desk.bus.subscribe(topic, self._guard(handler), owner=self.name)

    def _guard(self, handler: Callable[[Event], Any]) -> Callable[[Event], Any]:
        async def run(ev: Event) -> None:
            if not self.enabled:
                return
            res = handler(ev)
            if hasattr(res, "__await__"):
                await res

        return run

    async def on_cycle(self) -> None:
        """Called once per desk cycle after market data has been processed."""

    # -- helpers --------------------------------------------------------
    @property
    def settings(self):
        return self.desk.settings

    @property
    def portfolio(self):
        return self.desk.portfolio

    def publish(self, topic: str, payload: Any = None) -> None:
        self.desk.bus.publish(topic, payload, source=self.name)

    def act(self, message: str, level: str = "info") -> None:
        t = now()
        self.last_action = message
        self.last_action_ts = t
        entry = {"ts": t, "level": level, "message": message}
        self._log.append(entry)
        self._action_times.append(t)
        self.total_actions += 1
        self.desk.ui_emit({"type": "action", "bot": self.name, "title": self.title, **entry})

    def actions_per_min(self) -> float:
        cutoff = now() - 60
        return float(sum(1 for x in self._action_times if x >= cutoff))

    def full_log(self) -> list[dict[str, Any]]:
        return list(self._log)

    def bump(self, metric: str, by: float = 1) -> None:
        self.metrics[metric] = self.metrics.get(metric, 0) + by

    def state(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.errors and self.last_action_ts is None:
            return "error"
        return "active"

    def status(self) -> BotStatus:
        return BotStatus(
            self.name,
            self.role,
            self.title,
            self.description,
            self.enabled,
            self.state(),
            self.last_action,
            self.last_action_ts,
            self.errors,
            dict(self.metrics),
            list(self._log)[-15:],
            self.actions_per_min(),
            self.total_actions,
            self.mission,
        )
