"""The Trading Desk: wires market data, broker, event bus and the bot team."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .bots import TEAM
from .bots.base import Bot
from .brokers.base import Broker
from .config import Settings
from .core.bus import Event, EventBus
from .core.models import Alert
from .core.portfolio import Portfolio
from .journal import Journal
from .market.base import MarketFeed

log = logging.getLogger(__name__)


def build_feed(s: Settings) -> MarketFeed:
    if s.feed == "coinbase":
        from .market.coinbase import CoinbaseFeed

        return CoinbaseFeed(s.symbols)
    if s.feed == "alpaca":
        from .market.alpaca import AlpacaFeed

        return AlpacaFeed(s.symbols, s.alpaca_key, s.alpaca_secret)
    from .market.simulated import SimulatedFeed

    return SimulatedFeed(s.symbols, seed=s.seed)


def build_broker(s: Settings) -> Broker:
    if s.broker == "alpaca":
        from .brokers.alpaca import AlpacaBroker

        return AlpacaBroker(s.alpaca_key, s.alpaca_secret, paper=s.alpaca_paper)
    from .brokers.paper import PaperBroker

    return PaperBroker(s.commission_bps, s.slippage_bps)


class TradingDesk:
    def __init__(
        self,
        settings: Settings,
        feed: MarketFeed | None = None,
        broker: Broker | None = None,
        journal: Journal | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.bus = EventBus()
        self.bus.on_error = self._on_bot_error
        self.feed = feed or build_feed(settings)
        self.broker = broker or build_broker(settings)
        self.journal = journal or Journal(settings.journal_path)
        self.portfolio = Portfolio(settings.starting_cash)
        self.prices: dict[str, float] = {}
        self.paused = False
        self.pause_reason = ""
        self.halted = False
        self.halt_reason = ""
        self.cycles = 0
        self.started_at = time.time()
        self.running = False
        self._task: asyncio.Task | None = None
        self._day = self._today()
        self.listeners: list[Callable[[Event], None]] = []
        self.bots: dict[str, Bot] = {}
        for cls in TEAM:
            bot = cls(self)
            self.bots[bot.name] = bot
        for bot in self.bots.values():
            bot.attach()
        self.bus.subscribe("*", self._fanout, owner="desk")
        from .wallets import WalletService

        self.wallets = WalletService(self)

    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _on_bot_error(self, owner: str, ev: Event, exc: BaseException) -> None:
        bot = self.bots.get(owner)
        if bot:
            bot.errors += 1
            bot.act(f"error handling {ev.topic}: {exc}", "critical")

    def _fanout(self, ev: Event) -> None:
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # never let a UI client break the desk
                log.exception("listener failed")

    @property
    def is_live(self) -> bool:
        return self.settings.mode == "live" and self.broker.is_live

    # -- controls --------------------------------------------------------
    def control(self, action: str, reason: str = "operator") -> None:
        self.bus.publish("desk.control", {"action": action, "reason": reason}, source="operator")

    def pause(self, reason: str = "operator") -> None:
        self.paused = True
        self.pause_reason = reason
        self.control("pause", reason)

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""
        self.control("resume")

    def halt(self, reason: str, flatten: bool = True) -> None:
        self.halted = True
        self.halt_reason = reason
        self.control("halt", reason)
        if flatten:
            self.flatten(f"kill switch: {reason}")

    def reset_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""
        # re-baseline so the same loss doesn't instantly re-trip the switch
        self.portfolio.day_start_equity = self.portfolio.equity
        self.portfolio.peak_equity = self.portfolio.equity
        self.control("reset_halt")

    def flatten(self, reason: str = "operator flatten") -> int:
        pm = self.bots["portfolio"]
        n = pm.flatten_all(reason)  # type: ignore[attr-defined]
        self.control("flatten", reason)
        return n

    def set_bot_enabled(self, name: str, enabled: bool) -> None:
        bot = self.bots[name]
        bot.enabled = enabled
        bot.act("enabled by operator" if enabled else "disabled by operator", "warning")
        self.bus.publish("alert", Alert("warning" if not enabled else "info",
                                        f"{bot.title} {'enabled' if enabled else 'disabled'}", "operator"))

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        acct = await self.broker.account()
        if acct:
            # trade against the real account's buying power, not the configured number
            self.portfolio.cash = acct["cash"]
            self.portfolio.starting_cash = acct["equity"]
            self.portfolio.peak_equity = self.portfolio.day_start_equity = acct["equity"]
        self.bots["sentinel"].reset_shadow()  # type: ignore[attr-defined]
        mode = "LIVE" if self.is_live else "PAPER"
        self.bus.publish(
            "alert",
            Alert("info", f"Desk online in {mode} mode: feed={self.feed.name} broker={self.broker.name}", "desk"),
        )
        await self.bus.drain()

    async def step(self) -> None:
        """Run one full desk cycle."""
        today = self._today()
        if today != self._day:
            self._day = today
            self.portfolio.day_start_equity = self.portfolio.equity
        ticks = await self.feed.poll()
        self.broker.on_prices(ticks)
        for t in ticks:
            self.bus.publish("market.tick", t, source=self.feed.name)
        await self.bus.drain()
        for bot in self.bots.values():
            if not bot.enabled:
                continue
            try:
                await bot.on_cycle()
            except Exception as exc:
                bot.errors += 1
                bot.act(f"cycle error: {exc}", "critical")
                log.exception("bot %s cycle failed", bot.name)
            await self.bus.drain()
        self.cycles += 1

    async def run_forever(self) -> None:
        self.running = True
        await self.start()
        try:
            while self.running:
                t0 = time.monotonic()
                try:
                    await self.step()
                except Exception:
                    log.exception("desk cycle failed")
                await asyncio.sleep(max(0.0, self.settings.cycle_seconds - (time.monotonic() - t0)))
        finally:
            await self.shutdown()

    def stop(self) -> None:
        self.running = False

    async def shutdown(self) -> None:
        self.running = False
        await self.feed.close()
        await self.broker.close()
        await self.bots["grok"].close()  # type: ignore[attr-defined]
        self.journal.commit()

    # -- views -----------------------------------------------------------
    def state(self) -> dict:
        sentinel = self.bots["sentinel"]
        execution = self.bots["execution"]
        return {
            "mode": "live" if self.is_live else "paper",
            "feed": self.feed.name,
            "broker": self.broker.name,
            "paused": self.paused,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "cycles": self.cycles,
            "uptime": time.time() - self.started_at,
            "prices": self.prices,
            "portfolio": self.portfolio.to_dict(),
            "performance": sentinel.performance(),  # type: ignore[attr-defined]
            "bots": [vars(b.status()) for b in self.bots.values()],
            "orders": [o.to_dict() for o in list(execution.orders.values())[-50:]],  # type: ignore[attr-defined]
            "trades": [t.to_dict() for t in self.portfolio.closed_trades[-50:]],
            "events": list(sentinel.events)[-100:],  # type: ignore[attr-defined]
            "alerts": list(sentinel.alerts)[-30:],  # type: ignore[attr-defined]
            "regimes": {k: v.to_dict() for k, v in self.bots["strategist"].regimes.items()},  # type: ignore[attr-defined]
            "scores": self.bots["quant"].scores,  # type: ignore[attr-defined]
            "history": {s: self.bots["scout"].history(s)[-200:] for s in self.prices},  # type: ignore[attr-defined]
            "equity_curve": list(sentinel.equity_curve)[-500:],  # type: ignore[attr-defined]
            "wallets": self.wallets.to_dict(),
            "grok": {
                "mode": self.bots["grok"].mode,  # type: ignore[attr-defined]
                "model": self.settings.grok_model,
                "connected": self.bots["grok"].connected,  # type: ignore[attr-defined]
                "opinions": self.bots["grok"].opinions,  # type: ignore[attr-defined]
            },
            "accuracy": {
                "execution": self.bots["execution"].accuracy,  # type: ignore[attr-defined]
                "fills_verified": self.bots["execution"].metrics.get("fills_verified", 0),
                "integrity_violations": self.bots["execution"].metrics.get("integrity_violations", 0),
                "books_balanced": sentinel.books_balanced,  # type: ignore[attr-defined]
                "audit_checks": sentinel.metrics.get("audit_checks", 0),
                "pause_reason": self.pause_reason,
            },
        }
