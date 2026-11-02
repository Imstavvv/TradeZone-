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

        key, secret = s.data_keys()  # type: ignore[misc]
        return AlpacaFeed(s.symbols, key, secret)
    from .market.simulated import SimulatedFeed

    return SimulatedFeed(s.symbols, seed=s.seed)


def build_broker(s: Settings) -> Broker:
    if s.broker == "alpaca":
        from .brokers.alpaca import AlpacaBroker

        return AlpacaBroker(s.alpaca_key, s.alpaca_secret, paper=True)
    from .brokers.paper import PaperBroker

    return PaperBroker(s.fee_bps, s.slippage_bps)


def build_live_broker(s: Settings) -> Broker | None:
    if s.live_blockers():
        return None
    from .brokers.alpaca import AlpacaBroker

    return AlpacaBroker(s.alpaca_live_key, s.alpaca_live_secret, paper=False)


class ModeSwitchError(RuntimeError):
    pass


class TradingDesk:
    def __init__(
        self,
        settings: Settings,
        feed: MarketFeed | None = None,
        broker: Broker | None = None,
        journal: Journal | None = None,
        live_broker: Broker | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.bus = EventBus()
        self.bus.on_error = self._on_bot_error
        self.feed = feed or build_feed(settings)
        self.journal = journal or Journal(settings.journal_path)
        # Two separate books: paper is always available, live only once switched on.
        self.mode = "paper"
        self.paper_broker: Broker = broker or build_broker(settings)
        self.live_broker: Broker | None = live_broker or build_live_broker(settings)
        self.paper_portfolio = Portfolio(settings.starting_cash)
        self.live_portfolio: Portfolio | None = None
        self.portfolio = self.paper_portfolio
        self.mode_changed_at: float | None = None
        self.prices: dict[str, float] = {}
        self.paused = False
        self.pause_reason = ""
        self.halted = False
        self.halt_reason = ""
        self.halt_kind = ""  # daily | drawdown | manual
        self.cycles = 0
        self._dirty = True
        self.empty_polls = 0  # consecutive cycles without any market data
        self.last_tick_at: float | None = None
        self.feed_down = False
        self.started_at = time.time()
        self.running = False
        self._task: asyncio.Task | None = None
        self._day = self._today()
        self.listeners: list[Callable[[Event], None]] = []  # every bus event
        self.ui_listeners: list[Callable[[dict], None]] = []  # bot actions for the live page
        self._ui_rate: dict[str, tuple[float, int]] = {}
        self.bots: dict[str, Bot] = {}
        for cls in TEAM:
            bot = cls(self)
            self.bots[bot.name] = bot
        for bot in self.bots.values():
            bot.attach()
        self.bus.subscribe("*", self._fanout, owner="desk")
        for topic in ("order.filled", "desk.control", "funds.deposit", "funds.withdrawal"):
            self.bus.subscribe(topic, self._mark_dirty, owner="desk")
        from .persistence import StateStore

        self.store = StateStore(self.journal)
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

    UI_MAX_PER_BOT_PER_SEC = 8

    def ui_emit(self, msg: dict) -> None:
        """Push a bot action to live dashboards (throttled per bot so a busy bot can't flood them)."""
        if not self.ui_listeners:
            return
        sec = int(time.time())
        key = msg.get("bot", "")
        window, n = self._ui_rate.get(key, (sec, 0))
        if window != sec:
            window, n = sec, 0
        if n >= self.UI_MAX_PER_BOT_PER_SEC:
            return
        self._ui_rate[key] = (window, n + 1)
        for fn in list(self.ui_listeners):
            try:
                fn(msg)
            except Exception:
                log.exception("ui listener failed")

    def recent_actions(self, limit: int = 150) -> list[dict]:
        out = []
        for b in self.bots.values():
            out += [{"type": "action", "bot": b.name, "title": b.title, **e} for e in list(b._log)[-40:]]
        return sorted(out, key=lambda e: e["ts"])[-limit:]

    def _fanout(self, ev: Event) -> None:
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # never let a UI client break the desk
                log.exception("listener failed")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def broker(self) -> Broker:
        if self.mode == "live":
            assert self.live_broker is not None
            return self.live_broker
        return self.paper_broker

    @broker.setter
    def broker(self, b: Broker) -> None:
        if self.mode == "live":
            self.live_broker = b
        else:
            self.paper_broker = b

    # -- paper <-> live switching -------------------------------------------
    def _switch_blockers(self, target: str) -> list[str]:
        out: list[str] = []
        if target == "live":
            out += self.settings.live_blockers()
            if self.live_broker is None and not out:
                out.append("live broker unavailable")
            if self.halted:
                out.append("kill switch is active - reset it first")
            host = self.settings.host
            if host not in ("127.0.0.1", "localhost", "::1") and not self.settings.api_token:
                out.append(f"dashboard is exposed on {host} without TZ_API_TOKEN - set a token first")
        if self.portfolio.open_positions:
            out.append(f"close the {len(self.portfolio.open_positions)} open {self.mode} position(s) first (Flatten all)")
        if self.bots["execution"].working:  # type: ignore[attr-defined]
            out.append("wait for working orders to finish")
        return out

    async def mode_check(self) -> dict:
        """Preview for the Go LIVE dialog: what would block the switch, plus the live account."""
        target = "paper" if self.mode == "live" else "live"
        blockers = self._switch_blockers(target)
        account = None
        if target == "live" and self.live_broker is not None and not self.settings.live_blockers():
            acct = await self.live_broker.account()
            positions = await self.live_broker.positions()
            if acct is None:
                blockers.append("can't reach the Alpaca live account - check keys and network")
            else:
                account = {**acct, "positions": positions or {}}
                if positions:
                    blockers.append(
                        f"live account already holds {len(positions)} position(s); use a flat/dedicated account"
                    )
                cap = self.settings.live_max_capital
                account["trading_capital"] = min(acct["cash"], cap) if cap > 0 else acct["cash"]
        return {"mode": self.mode, "target": target, "blockers": blockers, "account": account,
                "live_max_capital": self.settings.live_max_capital,
                "risk": {k: getattr(self.settings.risk, k) for k in (
                    "risk_per_trade", "max_position_pct", "max_daily_loss_pct", "max_drawdown_pct")}}

    async def switch_mode(self, target: str, flatten: bool = False, timeout: float = 60.0) -> dict:
        """Switch books/brokers. With ``flatten``, new entries are paused and open positions are
        closed first; the previous pause state is restored afterwards."""
        if target not in ("paper", "live"):
            raise ModeSwitchError("mode must be 'paper' or 'live'")
        if target == self.mode:
            return {"mode": self.mode, "changed": False}
        if flatten and (self.portfolio.open_positions or self.bots["execution"].working):  # type: ignore[attr-defined]
            was_paused, prev_reason = self.paused, self.pause_reason
            self.pause(f"switching to {target}")
            try:
                self.flatten(f"switching to {target}")
                deadline = time.monotonic() + timeout
                while self.portfolio.open_positions or self.bots["execution"].working:  # type: ignore[attr-defined]
                    if time.monotonic() > deadline:
                        raise ModeSwitchError("positions did not close in time - check orders and try again")
                    await self.bus.drain()
                    if not self.running:  # no desk loop (tests / CLI): poll working orders ourselves
                        await self.bots["execution"].on_cycle()  # type: ignore[attr-defined]
                        await self.bus.drain()
                    if self.portfolio.open_positions:
                        await asyncio.sleep(0.25)
                return await self._switch(target)
            finally:
                self.paused, self.pause_reason = was_paused, prev_reason
        return await self._switch(target)

    async def _switch(self, target: str) -> dict:
        check = await self.mode_check()
        if check["blockers"]:
            raise ModeSwitchError("; ".join(check["blockers"]))
        if target == "live":
            acct = check["account"]
            capital = acct["trading_capital"]
            if capital <= 0:
                raise ModeSwitchError("live account has no cash to trade")
            book = Portfolio(capital)
            self.live_portfolio = book
            self.portfolio = book
        else:
            self.portfolio = self.paper_portfolio
        self.mode = target
        self.mode_changed_at = time.time()
        pm = self.bots["portfolio"]
        pm.exiting.clear()  # type: ignore[attr-defined]
        pm.trail_dist.clear()  # type: ignore[attr-defined]
        sentinel = self.bots["sentinel"]
        sentinel.reset_shadow()  # type: ignore[attr-defined]
        sentinel.equity_curve.clear()  # type: ignore[attr-defined]
        for t in self.prices.items():
            self.portfolio.mark(*t)
        level = "critical" if target == "live" else "info"
        msg = (f"LIVE TRADING ENABLED - trading capital {self.portfolio.cash:,.2f} USD via {self.broker.name}"
               if target == "live" else "Switched back to PAPER trading - no real orders will be sent")
        self.bus.publish("alert", Alert(level, msg, "operator"), source="operator")
        self.control(f"mode_{target}", msg)
        self.save_state()  # persist the switch immediately
        return {"mode": self.mode, "changed": True, "capital": self.portfolio.cash}

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

    def halt(self, reason: str, flatten: bool = True, kind: str = "manual") -> None:
        self.halted = True
        self.halt_reason = reason
        self.halt_kind = kind
        self.control("halt", reason)
        if flatten:
            self.flatten(f"kill switch: {reason}")

    def reset_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self.halt_kind = ""
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
    async def backfill(self, timeout: float = 20.0) -> int:
        """Pre-warm indicators from recent candles so the bots can trade right away."""
        scout = self.bots["scout"]
        gran = int(scout.bar_seconds)  # type: ignore[attr-defined]
        if gran <= 0:
            return 0

        async def one(sym: str) -> tuple[str, list[dict] | None]:
            return sym, await self.feed.history(sym, gran, 300)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(one(s) for s in self.settings.symbols)), timeout)
        except asyncio.TimeoutError:
            return 0
        total = 0
        for sym, candles in results:
            if candles:
                total += scout.backfill(sym, candles)  # type: ignore[attr-defined]
                await self.bus.drain()
        if total:
            self.bus.publish("alert", Alert(
                "info", f"Loaded {total} bars of recent history - bots ready to trade", "desk"))
        return total

    async def start(self) -> None:
        self.bots["grok"].load_saved()  # type: ignore[attr-defined]  # key saved from the dashboard, if any
        restored = await self.restore_state()
        await self.feed.start()
        await self.backfill()
        if restored:
            self.bus.publish("alert", Alert("info", f"Resumed after restart: {restored}", "desk"))
        if self.paper_broker.name == "alpaca" and not restored:
            acct = await self.paper_broker.account()
            if acct:  # mirror the Alpaca paper account's buying power
                p = self.paper_portfolio
                p.cash = p.starting_cash = p.peak_equity = p.day_start_equity = acct["cash"]
        self.bots["sentinel"].reset_shadow()  # type: ignore[attr-defined]
        if self.settings.mode == "live" and not self.is_live:
            try:
                await self.switch_mode("live")
            except ModeSwitchError as exc:
                self.bus.publish("alert", Alert("critical", f"Could not start LIVE, staying in PAPER: {exc}", "desk"))
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
            if self.halted and self.halt_kind == "daily" and self.settings.auto_rearm_daily:
                self.reset_halt()
                self.bus.publish("alert", Alert(
                    "warning", "New trading day: daily-loss kill switch re-armed automatically", "desk"))
        try:
            ticks = await self.feed.poll()
        except Exception as exc:  # a broken feed must never stop the desk loop
            self.feed.last_error = f"{type(exc).__name__}: {exc}"[:200]
            ticks = []
        self._track_feed(ticks)
        self.paper_broker.on_prices(ticks)
        if self.live_broker is not None:
            self.live_broker.on_prices(ticks)
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
        if self.cycles == 1 or self.cycles % 30 == 0:  # keep the status page fresh
            self.bots["scout"].metrics["feed_mode"] = getattr(self.feed, "mode", "poll")
        if self._dirty or self.cycles % 10 == 0:
            self.save_state()

    # -- persistence ---------------------------------------------------------
    def _mark_dirty(self, ev: Event) -> None:
        self._dirty = True

    def save_state(self) -> None:
        from .persistence import snapshot

        try:
            self.store.save("desk", snapshot(self))
            self._dirty = False
        except Exception:
            log.exception("could not save desk state")

    async def restore_state(self) -> str | None:
        """Restore books and mode after a restart. Returns a description, or None if nothing saved."""
        from .persistence import portfolio_from_state

        data = self.store.load("desk")
        if not data or not self.settings.resume_on_restart:
            return None
        self.paper_portfolio = self.portfolio = portfolio_from_state(data["paper"])
        self.paused, self.pause_reason = data.get("paused", False), data.get("pause_reason", "")
        self.halted, self.halt_reason = data.get("halted", False), data.get("halt_reason", "")
        self.halt_kind = data.get("halt_kind", "")
        if data.get("day") and data["day"] != self._day and self.halted and self.halt_kind == "daily" \
                and self.settings.auto_rearm_daily:
            self.halted, self.halt_reason, self.halt_kind = False, "", ""
        pm = self.bots["portfolio"]
        saved_pm = data.get("pm", {})
        msg = f"restored paper book ({len(self.paper_portfolio.open_positions)} open)"
        if data.get("mode") == "live" and data.get("live"):
            book = portfolio_from_state(data["live"])
            problem = await self._can_resume_live(book)
            if problem:
                self.bus.publish("alert", Alert(
                    "critical", f"Was LIVE before restart but could not resume: {problem}. Staying in PAPER - "
                    "check your broker account.", "desk"))
                msg += f"; live NOT resumed ({problem})"
            else:
                self.live_portfolio = self.portfolio = book
                self.mode = "live"
                self.mode_changed_at = time.time()
                msg += f"; resumed LIVE ({len(book.open_positions)} open)"
        active = {p.symbol for p in self.portfolio.open_positions}
        pm.trail_dist.update({k: v for k, v in saved_pm.get("trail_dist", {}).items() if k in active})  # type: ignore[attr-defined]
        pm.protected.update(s for s in saved_pm.get("protected", []) if s in active)  # type: ignore[attr-defined]
        pm.scaled_out.update(s for s in saved_pm.get("scaled_out", []) if s in active)  # type: ignore[attr-defined]
        self.bots["sentinel"].reset_shadow()  # type: ignore[attr-defined]
        return msg

    async def _can_resume_live(self, book: Portfolio) -> str | None:
        if self.settings.live_blockers() or self.live_broker is None:
            return "live trading is not configured"
        acct = await self.live_broker.account()
        if acct is None:
            return "live account unreachable"
        theirs = await self.live_broker.positions() or {}
        norm = lambda s: s.replace("-", "").replace("/", "").upper()  # noqa: E731
        ours = {norm(p.symbol): p.qty for p in book.open_positions}
        theirs = {norm(k): v for k, v in theirs.items() if abs(v) > 1e-12}
        for sym in set(ours) | set(theirs):
            a, b = ours.get(sym, 0.0), theirs.get(sym, 0.0)
            if abs(a - b) > 1e-6 * max(1.0, abs(b)):
                return f"broker shows {sym} {b:g} but saved book has {a:g}"
        return None

    FEED_DOWN_AFTER = 10  # cycles without data before alerting

    def _track_feed(self, ticks: list) -> None:
        if ticks:
            self.last_tick_at = time.time()
            self.empty_polls = 0
            if self.feed_down:
                self.feed_down = False
                self.bus.publish("alert", Alert("info", f"Market data from {self.feed.name} restored", "desk"))
            return
        self.empty_polls += 1
        if self.empty_polls >= self.FEED_DOWN_AFTER and not self.feed_down:
            self.feed_down = True
            why = self.feed.last_error or "no response"
            self.bus.publish("alert", Alert(
                "critical", f"No market data from {self.feed.name} for {self.empty_polls} cycles: {why}", "desk"))

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
        await self.paper_broker.close()
        if self.live_broker is not None:
            await self.live_broker.close()
        await self.bots["grok"].close()  # type: ignore[attr-defined]
        self.journal.commit()

    # -- views -----------------------------------------------------------
    def state(self) -> dict:
        sentinel = self.bots["sentinel"]
        execution = self.bots["execution"]
        return {
            "mode": "live" if self.is_live else "paper",
            "feed": self.feed.name,
            "feed_status": {
                "ok": not self.feed_down,
                "mode": getattr(self.feed, "mode", "poll"),  # stream | rest | poll
                "error": self.feed.last_error,
                "last_tick_at": self.last_tick_at,
                "warming_up": self.bots["scout"].warmup_status(),  # type: ignore[attr-defined]
            },
            "broker": self.broker.name,
            "live": {
                "available": not self.settings.live_blockers(),
                "config_blockers": self.settings.live_blockers(),
                "since": self.mode_changed_at if self.is_live else None,
                "max_capital": self.settings.live_max_capital,
            },
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
            "actions": self.recent_actions(),
            "alerts": list(sentinel.alerts)[-30:],  # type: ignore[attr-defined]
            "regimes": {k: v.to_dict() for k, v in self.bots["strategist"].regimes.items()},  # type: ignore[attr-defined]
            "scores": self.bots["quant"].scores,  # type: ignore[attr-defined]
            "history": {s: self.bots["scout"].history(s)[-200:] for s in self.prices},  # type: ignore[attr-defined]
            "equity_curve": list(sentinel.equity_curve)[-500:],  # type: ignore[attr-defined]
            "wallets": self.wallets.to_dict(),
            "grok": {
                **self.bots["grok"].info(),  # type: ignore[attr-defined]  (never includes the key itself)
                "opinions": self.bots["grok"].opinions,  # type: ignore[attr-defined]
            },
            "executive": self.bots["executive"].summary(),  # type: ignore[attr-defined]
            "accuracy": {
                "execution": self.bots["execution"].accuracy,  # type: ignore[attr-defined]
                "fills_verified": self.bots["execution"].metrics.get("fills_verified", 0),
                "integrity_violations": self.bots["execution"].metrics.get("integrity_violations", 0),
                "books_balanced": sentinel.books_balanced,  # type: ignore[attr-defined]
                "audit_checks": sentinel.metrics.get("audit_checks", 0),
                "pause_reason": self.pause_reason,
            },
        }
