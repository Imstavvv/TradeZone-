"""The Trading Desk: wires market data, broker, event bus and the bot team."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import ClassVar

from .bots import TEAM
from .bots.base import Bot
from .brokers.base import Broker
from .config import VENUES, Settings
from .core.bus import Event, EventBus
from .core.models import Alert
from .core.portfolio import Portfolio
from .core.util import norm_symbol
from .journal import Journal
from .market.base import MarketFeed

log = logging.getLogger(__name__)


def build_feed(s: Settings) -> MarketFeed:
    if s.feed == "kraken":
        from .config import MEME_CANDIDATES, SMALL_CANDIDATES
        from .market.kraken import KrakenFeed

        return KrakenFeed(s.symbols, meme_candidates=MEME_CANDIDATES if s.kraken_memes else [],
                          meme_coins=s.meme_coins, min_meme_volume_usd=s.min_meme_volume_usd,
                          max_memes=s.max_meme_coins,
                          small_candidates=SMALL_CANDIDATES if s.focus_small_coins else [], max_small=s.max_small_coins,
                          all_coins=s.kraken_all_coins, max_all=s.max_kraken_coins, min_all_volume_usd=s.min_coin_volume_usd)
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
    if s.venue() == "uphold":
        from .brokers.uphold import UpholdBroker

        return UpholdBroker(s.uphold_token, symbols=s.symbols)
    if s.venue() == "kraken":
        from .brokers.kraken import KrakenBroker

        return KrakenBroker(s.kraken_key, s.kraken_secret)
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
        self._feed_given = feed is not None  # a feed passed in (tests, demos) is never swapped
        self.journal = journal or Journal(settings.journal_path or "tradezone.db")
        # Two separate books: paper is always available, live only once switched on.
        self.mode = "paper"
        self.paper_broker: Broker = broker or build_broker(settings)
        self.live_broker: Broker | None = live_broker or build_live_broker(settings)
        self._tune_live_broker()
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
        self.advisors = [b for b in self.bots.values() if getattr(b, "advisor", False)]
        for name in settings.bots_off:  # (failed on unseen data: off unless switched on from the team page)
            if name in self.bots and not self.bots[name].critical:
                self.bots[name].enabled = False
        self.bus.subscribe("*", self._fanout, owner="desk")
        for topic in ("order.filled", "desk.control", "funds.deposit", "funds.withdrawal"):
            self.bus.subscribe(topic, self._mark_dirty, owner="desk")
        self.hunt = self._new_hunt()
        self._pnl_key: tuple | None = None  # (mode, book) the chart's P&L offset was worked out for
        self._pnl_offset = 0.0
        self.floor_ack: dict[str, float] = {}  # mode -> equity the owner restarted from after a capital-floor stop
        self.goal: dict = {}
        for topic in ("market.snapshot", "signal.proposed", "signal.rejected", "order.held", "trade.log"):
            self.bus.subscribe(topic, self._count_hunt, owner="desk")
        # the US day-trade rule (stocks, under $25,000): what was bought today, and the day trades made
        self.day_trades: dict[str, list[float]] = {"paper": [], "live": []}
        self._bought_on: dict[str, dict[str, str]] = {"paper": {}, "live": {}}
        self.bus.subscribe("trade.log", self._track_day_trades, owner="desk")
        from .persistence import StateStore

        self.store = StateStore(self.journal)
        from .wallets import WalletService

        self.wallets = WalletService(self)
        from .fleet import AccountFleet

        self.fleet = AccountFleet(self)  # extra real-money accounts, each with its own bot team
        from .memedesk import MemeDesk
        self.memedesk = MemeDesk()  # the MemeCoin desk, attached read-only (its address is saved from Settings)

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

    def lifetime_pnl(self) -> float:
        """Profit/loss of the current mode since it was first used: this book's result plus what earlier
        books of the same mode made (each real-money session starts a new book). Deposits don't count."""
        p = self.portfolio
        session = p.equity - p.starting_cash
        if self._pnl_key != (self.mode, id(p)):
            last = self.journal.last_pnl(self.mode)
            self._pnl_offset = (last - session) if last is not None else 0.0
            self._pnl_key = (self.mode, id(p))
        return self._pnl_offset + session

    def chart(self, mode: str | None = None, hours: float = 1.0) -> dict:
        """The live profit/loss chart: P&L over time plus every trade on it, for practice or real money."""
        mode = mode if mode in ("paper", "live") else self.mode
        until = time.time()
        since = until - hours * 3600 if hours > 0 else 0.0
        points = self.journal.pnl_series(mode, since, until)
        start = points[0][0] if points else until
        now_pnl = self.lifetime_pnl() if mode == self.mode else (points[-1][1] if points else 0.0)
        if mode == self.mode:
            points.append([until, now_pnl])
        trades = self.journal.trades_since(mode, start if hours <= 0 else since)
        closed = [t for t in trades if t.get("pnl") is not None]
        return {"mode": mode, "current": mode == self.mode, "hours": hours, "points": points, "trades": trades,
                "pnl": now_pnl, "change": now_pnl - points[0][1] if points else 0.0,
                "realized": sum(t["pnl"] for t in closed), "fees": sum(t.get("fee") or 0 for t in trades),
                "wins": sum(t["pnl"] > 0 for t in closed), "closed": len(closed)}

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
                out.append("the emergency stop (kill switch) is on - tap Restart trading first")
            if not self.owner_protected():
                out.append("log in to the website first (with the login code from your server logs), so only you can control real money")
        if self.portfolio.open_positions:
            out.append(f"close your {len(self.portfolio.open_positions)} open {self.mode} position(s) first (tap Close all trades)")
        if self.bots["execution"].working:  # type: ignore[attr-defined]
            out.append("wait for working orders to finish")
        if self.bots["stacker"].pending is not None:  # type: ignore[attr-defined]
            out.append("wait a moment: a hold-stack purchase is still filling")
        return out

    async def mode_check(self) -> dict:
        """Preview for the Go LIVE dialog: what would block the switch, plus the live account."""
        target = "paper" if self.mode == "live" else "live"
        blockers = self._switch_blockers(target)
        account = None
        if target == "live" and self.live_broker is not None and not self.settings.live_blockers():
            acct = await self.live_broker.account()
            kraken = hasattr(self.live_broker, "capture_baseline")
            # Kraken: coins you already hold are noted and left alone, so they don't block anything
            positions = {} if kraken else await self.live_broker.positions()
            if positions:  # coins in your hold stack are yours: they don't block anything
                stack = self.bots["stacker"].held("live")  # type: ignore[attr-defined]
                positions = {k: v for k, v in positions.items()
                             if abs(v - stack.get(norm_symbol(k), 0.0)) > 1e-9 * max(1.0, abs(v))}
            if acct is not None and acct.get("cash", 0) < 1 and acct.get("stablecoins"):
                coins = ", ".join(f"{v:,.2f} {k}" for k, v in acct["stablecoins"].items())
                blockers.append(f"your {self.live_broker.label} account has no US dollars to trade with (it holds {coins}) - "
                                f"convert some to USD in {self.live_broker.label} first; the bots trade the USD pairs")
            if acct is None:
                blockers.append(f"couldn't reach your {self.live_broker.label} account - check it in Settings → Linked accounts")
            else:
                account = {**acct, "positions": positions or {}, "venue": self.live_broker.label}
                if positions:
                    blockers.append(
                        f"your {self.live_broker.label} account already holds {len(positions)} position(s) of coins the "
                        f"bots trade - sell them in {self.live_broker.label} first "
                        "(TradeZone needs an account with nothing open)"
                    )
                cap = self.settings.live_max_capital
                account["trading_capital"] = min(acct["cash"], cap) if cap > 0 else acct["cash"]
        return {"mode": self.mode, "target": target, "blockers": blockers, "account": account,
                "live_max_capital": self.settings.live_max_capital,
                "risk": self.risk_info(live=True)}

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
            if hasattr(self.live_broker, "capture_baseline"):  # Kraken: never touch the coins you already had
                try:
                    baseline = await self.live_broker.capture_baseline()  # type: ignore[union-attr]
                except Exception as exc:
                    raise ModeSwitchError(f"couldn't read your {self.live_broker.label} balances: {exc}") from None
                self._save_account_row("kraken_baseline", baseline)
            book = Portfolio(capital)
            self.live_portfolio = book
            self.portfolio = book
            self.bots["portfolio"].outside.clear()  # type: ignore[attr-defined]  # a fresh live book: recount
            if not hasattr(self.live_broker, "capture_baseline"):  # (Kraken's baseline already holds them)
                self.bots["portfolio"].outside.update(self.bots["stacker"].held("live"))  # type: ignore[attr-defined]
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
        if self.halt_kind == "floor":  # the owner chose to carry on from here: this is the new floor base
            self.floor_ack[self.mode] = self.portfolio.equity
        self.halted = False
        self.halt_reason = ""
        self.halt_kind = ""
        # re-baseline so the same loss doesn't instantly re-trip the switch
        self.portfolio.day_start_equity = self.portfolio.equity
        self.portfolio.peak_equity = self.portfolio.equity
        self.control("reset_halt")

    # -- one trade at a time (its own buttons on the open-trades list) ----------------------------
    def _open_trade(self, symbol: str):
        sym = symbol.strip().upper()
        pos = self.portfolio.position(sym)
        if pos.is_flat:
            raise ValueError(f"there's no open {sym.split('-')[0]} trade")
        return sym, pos

    def sell_trade(self, symbol: str, fraction: float = 1.0, confirm_loss: bool = False) -> dict:
        """Sell one open trade, or part of it - your own sale: like Close all, it may sell below what was paid, but
        only once you've confirmed that (the reply says what it would lose)."""
        sym, pos = self._open_trade(symbol)
        frac = max(0.0, min(1.0, float(fraction)))
        if frac <= 0:
            raise ValueError("choose how much to sell")
        price = self.prices.get(sym) or pos.last_price
        net = self.settings.net_if_sold(pos, price) * frac if price else 0.0
        if net < 0 and not confirm_loss:
            return {"symbol": sym, "needs_confirm": True, "net": net, "fraction": frac}
        pm = self.bots["portfolio"]
        pm.exiting.discard(sym)  # type: ignore[attr-defined]
        pm._retry_at.pop(sym, None)  # type: ignore[attr-defined]
        pm._exit(sym, f"sold by you ({'all' if frac >= 1 else f'{frac:.0%}'})", fraction=frac, force=True)  # type: ignore[attr-defined]
        return {"symbol": sym, "selling": frac, "net": net}

    def coin_read(self, symbol: str) -> dict:
        """What the team sees in one coin right now: the bull vs bear debate, the whales, Grok's chart read and the
        chart picture itself (a PNG, base64)."""
        import base64
        sym = symbol.strip().upper()
        snap = self.bots["risk"].snapshots.get(sym)
        sm, cv, db = self.bots.get("smartmoney"), self.bots.get("chartvision"), self.bots.get("debate")
        out: dict = {"symbol": sym, "price": self.prices.get(sym)}
        if db is not None and snap is not None:
            case = db.debate(sym, snap)
            out["debate"] = {"verdict": case["verdict"], "bull_score": case["bull_score"], "bear_score": case["bear_score"],
                             "bull": [p for _, p in case["bull"]], "bear": [p for _, p in case["bear"]]}
        if sm is not None:
            out["smart_money"] = sm.read(sym)
        if cv is not None:
            out["vision"] = cv.read(sym)
            out["vision_status"] = cv.vision_status()
            png = cv.picture(sym)
            out["chart_png"] = base64.b64encode(png).decode() if png else None
            out["chart_frame"] = self.settings.chart_vision_frame
        return out

    def look_now(self, symbol: str) -> bool:
        """Have Grok read this coin's chart now (one of the hour's looks). False: no Grok key, no chart yet, or the
        hour's looks are used up."""
        cv = self.bots.get("chartvision")
        sym = symbol.strip().upper()
        if cv is None or (client := cv.client()) is None or cv.budget_left() <= 0 or (png := cv.picture(sym)) is None:
            return False
        cv.reads.pop(sym, None)
        cv.sent.append(time.time())
        task = asyncio.ensure_future(cv._look(client, sym, png))
        cv._tasks.add(task)
        task.add_done_callback(cv._tasks.discard)
        return True

    def set_trade_plan(self, symbol: str, stop_pct: float | None = None, target_pct: float | None = None) -> dict:
        """Change one trade's stop-loss and / or take-profit, as a share of what was paid. The stop can't be looser
        than the most a trade may lose (-7%), and the target is counted after both fees, like every target."""
        sym, pos = self._open_trade(symbol)
        s = self.settings
        most = max(s.wide_stop_pct, s.max_loss_pct) if s.max_loss_pct > 0 else s.wide_stop_pct
        if stop_pct is not None:
            stop_pct = float(stop_pct)
            if not 0 < stop_pct <= most + 1e-9:
                raise ValueError(f"the stop-loss must be between 0% and {most:.0%} below what was paid")
            stop = pos.avg_price * (1 - stop_pct)
            price = self.prices.get(sym) or pos.last_price
            if price and stop >= price:
                raise ValueError("that stop is above the price now - it would sell straight away")
            pos.stop_price = stop
            self.bots["portfolio"].trail_dist[sym] = pos.avg_price * stop_pct  # type: ignore[attr-defined]
        if target_pct is not None:
            target_pct = float(target_pct)
            if not 0 < target_pct <= 5:
                raise ValueError("the take-profit must be between 0% and 500% above what was paid")
            pos.take_profit = pos.avg_price * (1 + target_pct) + s.round_trip_cost(sym, pos.avg_price)  # (after fees)
        self.save_state()
        self.bus.publish("alert", Alert("info", f"{sym}: your plan - stop {pos.stop_price or 0:,.6g}, target "
                                                f"{pos.take_profit or 0:,.6g}", "operator"))
        return {"symbol": sym, "stop_price": pos.stop_price, "take_profit": pos.take_profit}

    async def attach_memedesk(self, url: str) -> dict | None:
        """Attach (or, with an empty address, detach) the MemeCoin desk; read it once straight away."""
        self.memedesk.set_url(url)  # (ValueError: the address isn't accepted)
        self.store.save("memedesk", {"url": self.memedesk.url})
        if self.memedesk.url:
            await self.memedesk.refresh()
        return self.memedesk.view()

    def flatten(self, reason: str = "operator flatten") -> int:
        pm = self.bots["portfolio"]
        n = pm.flatten_all(reason)  # type: ignore[attr-defined]
        self.control("flatten", reason)
        return n

    def set_bot_enabled(self, name: str, enabled: bool) -> None:
        bot = self.bots[name]
        if name == "stacker":  # pausing stops new purchases only: an order in flight is still followed to the end
            self.settings.stack = enabled
            bot.save()  # type: ignore[attr-defined]
        else:
            bot.enabled = enabled
        bot.act("enabled by operator" if enabled else "disabled by operator", "warning")
        self.bus.publish("alert", Alert("warning" if not enabled else "info",
                                        f"{bot.title} {'enabled' if enabled else 'disabled'}", "operator"))

    # -- lifecycle -------------------------------------------------------
    BACKFILL_FIRST = 40  # coins whose history loads before trading starts (the starting list and the busiest)

    TF_HISTORY_GAP_S = 1.0

    async def _backfill_timeframes(self, syms: list[str]) -> None:
        """Hourly and daily candles from the exchange for every coin (held coins first), so the long timeframes have
        their real history from the start - in the background, paced by the exchange."""
        scout = self.bots["scout"]
        held = {p.symbol for p in self.portfolio.open_positions}
        for sym in sorted(syms, key=lambda x: x not in held):
            for label, gran in (("1h", 3600), ("1d", 86_400)):
                if not self.running and self.cycles > 0:
                    return
                try:
                    candles = await self.feed.history(sym, gran, 300)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("%s history for %s failed: %s", label, sym, exc)
                    continue
                if candles and sym in self.settings.symbols:
                    scout.seed_timeframe(sym, label, candles)  # type: ignore[attr-defined]
                await asyncio.sleep(self.TF_HISTORY_GAP_S)  # (background: leaves the exchange's public limit to the prices)

    async def _backfill_more(self, syms: list[str], gran: int) -> None:
        """The rest of the coins' history, one at a time in the background (the exchange paces it anyway): each coin
        can be traded as soon as its own history is in, and the team trades the others meanwhile."""
        scout = self.bots["scout"]
        for sym in syms:
            if not self.running and self.cycles > 0:
                return
            try:
                candles = await self.feed.history(sym, gran, 300)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # (one coin's history failing never stops the others)
                log.warning("history for %s failed: %s", sym, exc)
                continue
            if candles and sym in self.settings.symbols:
                scout.backfill(sym, candles)  # type: ignore[attr-defined]  # (its events are handled on the next cycle)

    async def backfill(self, timeout: float = 20.0) -> int:
        """Pre-warm indicators from recent candles so the bots can trade right away."""
        scout = self.bots["scout"]
        gran = int(scout.bar_seconds)  # type: ignore[attr-defined]
        if gran <= 0:
            return 0

        async def one(sym: str) -> tuple[str, list[dict] | None]:
            return sym, await self.feed.history(sym, gran, 300)

        # each coin's history is kept as it arrives: one slow (or paced) coin no longer throws away all the others
        syms = list(self.settings.symbols)
        everyone, rest = list(syms), []
        if len(syms) > self.BACKFILL_FIRST:  # every coin on Kraken: the first ones now, the rest join as they load
            rest, syms = syms[self.BACKFILL_FIRST:], syms[: self.BACKFILL_FIRST]
        if rest or self.settings.timeframe_history:
            old = self.__dict__.get("_backfill_rest")
            if old is not None and not old.done():
                old.cancel()

            async def background() -> None:
                if rest:
                    await self._backfill_more(rest, gran)
                if self.settings.timeframe_history:  # then the long timeframes' own history
                    await self._backfill_timeframes(everyone)
            self._backfill_rest = asyncio.ensure_future(background())
        tasks = [asyncio.ensure_future(one(s)) for s in syms]
        done, pending = await asyncio.wait(tasks, timeout=max(timeout, 1.5 * len(syms)))
        for t in pending:
            t.cancel()
        results = [t.result() for t in done if not t.cancelled() and t.exception() is None]
        total = 0
        for sym, candles in results:
            if candles:
                total += scout.backfill(sym, candles)  # type: ignore[attr-defined]
                await self.bus.drain()
        if total:
            self.bus.publish("alert", Alert(
                "info", f"Loaded {total} bars of recent history - bots ready to trade", "desk"))
        return total

    # -- linked real-money broker (connected from the app, stored server-side) ----------
    def _accounts_table(self):
        with self.journal._lock:
            self.journal.conn.execute("CREATE TABLE IF NOT EXISTS linked_accounts (kind TEXT PRIMARY KEY, data TEXT)")
            self.journal.conn.commit()

    # A real-money account linked in the app is stored server-side (never sent back to the browser).
    # Rows: 'alpaca_live' / 'kraken_live' / 'uphold_live' (only one at a time) and 'kraken_baseline'.
    LIVE_ROWS = ("alpaca_live", "kraken_live", "uphold_live", "kraken_baseline")

    def _account_row(self, kind: str) -> dict | None:
        import json as _json
        self._accounts_table()
        row = self.journal.one("SELECT data FROM linked_accounts WHERE kind = ?", (kind,))
        return _json.loads(row[0]) if row else None

    def _save_account_row(self, kind: str, data: dict | None) -> None:
        import json as _json
        self._accounts_table()
        if data is None:
            self.journal.run("DELETE FROM linked_accounts WHERE kind = ?", (kind,))
        else:
            self.journal.run("INSERT OR REPLACE INTO linked_accounts(kind, data) VALUES (?, ?)", (kind, _json.dumps(data)))

    def _use_venue(self, venue: str, data: dict) -> None:
        """Remember one real-money account (forgetting any other) and point the settings at it."""
        for kind in self.LIVE_ROWS:
            self._save_account_row(kind, None)
        self._save_account_row(f"{venue}_live", data)
        if venue == "alpaca":  # (linked on purpose: the one-time unlink never undoes it)
            self.store.save("alpaca_unlinked", {"done": True})
        s = self.settings
        self._clear_live_credentials()
        if venue == "uphold":
            s.uphold_token = data["token"]
        elif venue == "kraken":
            s.kraken_key, s.kraken_secret = data["key"], data["secret"]
        else:
            s.alpaca_live_key, s.alpaca_live_secret = data["key"], data["secret"]
        s.live_venue = venue
        s.apply_venue_costs()

    def _clear_live_credentials(self) -> None:
        s = self.settings
        s.alpaca_live_key = s.alpaca_live_secret = s.kraken_key = s.kraken_secret = s.uphold_token = ""

    async def _adopt_live_broker(self, broker: Broker) -> None:
        old, self.live_broker = self.live_broker, broker
        self.broker_source = "app"
        if old is not None and old is not broker:
            await old.close()
        self._tune_live_broker()
        if self.running:  # linked while running: switch to the venue's own prices if they differ
            await self._venue_feed(restart=True)

    def _tune_live_broker(self) -> None:
        b, s = self.live_broker, self.settings
        if b is None:
            return
        b.symbols = s.symbols  # type: ignore[attr-defined]  # the same list: coins the feed adds are checked too
        if hasattr(b, "crypto_fee_bps"):
            b.crypto_fee_bps = s.crypto_fee_bps  # Alpaca takes its crypto fee from what you receive
        for k, v in (("max_spread_pct", s.sell_max_spread_pct), ("min_volume_usd", s.sell_min_volume_usd),
                     ("max_volume_share", s.sell_max_volume_share)):
            if hasattr(b, k):
                setattr(b, k, v)

    async def _venue_feed(self, restart: bool = False) -> None:
        """With feed=auto and a Kraken account, use Kraken's own prices (its meme coins have prices then)."""
        s = self.settings
        if self._feed_given or s.feed_choice != "auto":
            return
        want = "kraken" if s.venue() == "kraken" and self.live_broker is not None else s.feed
        if want == self.feed.name:
            return
        s.feed = want
        old, self.feed = self.feed, build_feed(s)
        await old.close()
        if restart:
            await self.feed.start()
            await self.backfill()
            self.bus.publish("alert", Alert("info", f"Now using {self.feed.name.title()}'s own prices", "desk"))

    # -- growth ladder: focus on smaller coins until the balance can spread across all of them ------------
    def focus_status(self) -> dict:
        s, eq = self.settings, self.portfolio.equity
        key = (self.cycles, eq, s.focus_small_coins, s.focus_small_until, s.focus_mid_until, len(s.symbols))
        if getattr(self, "_focus_key", None) == key:  # asked ~30 times a cycle: work it out once
            return self._focus_cache
        self._focus_key, self._focus_cache = key, self._focus_status(s, eq)
        return self._focus_cache

    def _focus_status(self, s, eq) -> dict:
        if not s.focus_small_coins:
            stage = 3
        else:
            stage = 1 if eq < s.focus_small_until else 2 if eq < s.focus_mid_until else 3
        nxt = {1: s.focus_small_until, 2: s.focus_mid_until}.get(stage)
        allowed = {"small"} | ({"mid"} if stage >= 2 else set()) | ({"large"} if stage >= 3 else set())
        return {"on": s.focus_small_coins, "stage": stage, "allowed": sorted(allowed), "next_at": nxt,
                "small_until": s.focus_small_until, "mid_until": s.focus_mid_until,
                "max_positions": None, "equity": eq,  # (trades open at once: the risk level's limit, up to 100)
                "coins": [x for x in s.symbols if s.coin_size(x) in allowed or x.split("-")[0] in s.always_trade],
                "always": list(s.always_trade)}

    def _underwater(self, pos, price: float) -> bool:
        """More than ``loser_cap_depth`` below break-even after fees. (With typical fees a trade starts about
        there, so in practice: trades that haven't yet made back their fees count - that is what was tested.)"""
        return self.settings.net_if_sold(pos, price) < -self.settings.loser_cap_depth * abs(pos.qty * pos.avg_price)

    def _past_grace(self, pos) -> bool:
        """Open long enough to count toward the underwater cap (measured on the market's clock)."""
        grace = self.settings.loser_cap_grace_min * 60
        if grace <= 0:
            return True
        seen = self.__dict__.setdefault("_uw_seen", {})
        key = (pos.opened_ts, pos.qty > 0)
        if seen.get(pos.symbol, (None,))[0] != key:  # (a new trade in this coin: its clock starts now)
            seen[pos.symbol] = (key, self._mkt_ts())
        return self._mkt_ts() - seen[pos.symbol][1] >= grace

    def _mkt_ts(self) -> float:
        return getattr(self, "_last_tick_ts", 0.0) or time.time()

    def underwater_cost(self, grace: bool = False) -> float:
        """What was paid for the open trades now held below break-even after fees."""
        return sum(abs(x.qty * x.avg_price) for x in self.portfolio.open_positions
                   if (px := self.prices.get(x.symbol)) and self._underwater(x, px) and (not grace or self._past_grace(x)))

    def underwater_block(self) -> str | None:
        """The underwater cap: why new buys wait (too much is tied up in trades held below break-even), or None."""
        s = self.settings
        if s.loser_cap <= 0:
            return None
        p = self.portfolio
        stuck = self.underwater_cost(grace=True)
        equity = max(p.equity, 1e-9)
        if stuck >= s.loser_cap * equity:
            return (f"underwater cap: {stuck / equity:.0%} of the balance is in trades held below break-even "
                    f"(cap {s.loser_cap:.0%}) - no new buys until some recover")
        return None

    def in_focus(self, symbol: str) -> str | None:
        """Why a NEW trade in ``symbol`` waits for a bigger balance, or None when it's in focus."""
        if symbol.upper().replace("/", "-").split("-")[0] in self.settings.always_trade or self.settings.is_forex(symbol):
            return None  # (traded at any balance; a currency pair isn't a coin on the ladder)
        f = self.focus_status()
        size = self.settings.coin_size(symbol)
        if size in f["allowed"]:
            return None
        need = self.settings.focus_mid_until if size == "large" else self.settings.focus_small_until
        return (f"focusing on smaller coins to build capital: {symbol} joins when the balance reaches "
                f"${need:,.0f} (now ${f['equity']:,.2f})")

    # -- after a restart: what's held is watched again ------------------------------------
    def _watch_held(self) -> list[str]:
        """After a restart: what's still held (open trades in either book, the hold stack) is watched again.

        Coins and stocks the Trend Scout found aren't on the starting list. Without this a held one got no
        prices after a restart - its stop couldn't fire, it couldn't be sold, and its value froze. Each is handed
        back to the scout, so it's dropped again once it's sold and no longer qualifies."""
        s = self.settings
        held: set[str] = set()
        for book in (self.paper_portfolio, self.live_portfolio):
            if book is not None:
                held |= {p.symbol for p in book.open_positions}
        st = self.bots.get("stacker")
        for b in (getattr(st, "books", None) or {}).values():
            held |= {sym for sym, h in b.items() if h.get("qty", 0) > 0}
        back = []
        trends = self.bots.get("trends")
        for sym in sorted(held):
            if sym in s.symbols or (s.feed in ("coinbase", "kraken") and not sym.endswith("-USD")):
                continue  # (a stock can't be priced by a crypto feed)
            s.symbols.append(sym)  # the same list the feed, broker and bots use
            back.append(sym)
            if trends is not None:
                (trends.added if s.is_crypto(sym) else trends.stocks_added)[sym] = time.time()
        return back

    def stock_market_open(self) -> bool:
        """US stock market open now? Alpaca's clock when linked (holidays, early closes), else weekdays 9:30-16:00."""
        from .core.util import us_market_open
        clock = getattr(self.live_broker, "market_open", None)
        said = clock() if callable(clock) else None
        return said if said is not None else us_market_open()

    # -- the US day-trade rule: stocks, accounts under $25,000 -----------------------------------
    PDT_EQUITY = 25_000.0
    PDT_LIMIT = 3  # day trades allowed in 5 trading days

    @staticmethod
    def _ny_day(ts: float) -> str:
        from datetime import datetime

        from .core.util import _NY
        return datetime.fromtimestamp(ts, _NY).date().isoformat()

    def _track_day_trades(self, ev) -> None:
        p = ev.payload
        sym, ts = p.get("symbol", ""), float(p.get("ts") or time.time())
        if not sym or self.settings.is_crypto(sym):
            return
        if p.get("reason") == "moved by you between the hold stack and the trading":
            return  # a hand move to or from the hold stack: no order reaches the exchange, so no day trade
        mode = p.get("mode") or self.mode
        day = self._ny_day(ts)
        bought = self._bought_on.setdefault(mode, {})
        if p.get("action") in ("open", "add", "flip"):
            if p.get("action") == "flip" and bought.get(sym) == day:
                self._log_day_trade(mode, ts)
            bought[sym] = day
        elif p.get("action") in ("close", "partial", "reduce") and bought.get(sym) == day:
            self._log_day_trade(mode, ts)
        self.store.save("day_trades", {"trades": self.day_trades, "bought": self._bought_on})

    def _log_day_trade(self, mode: str, ts: float) -> None:
        self.day_trades.setdefault(mode, []).append(ts)
        self.day_trades[mode] = [t for t in self.day_trades[mode] if t > ts - 10 * 86_400][-50:]

    def day_trades_used(self, mode: str | None = None) -> int:
        """Day trades in the last 5 trading days (ours, or Alpaca's own count when it's higher)."""
        from datetime import date, timedelta
        mode = mode or self.mode
        today = date.fromisoformat(self._ny_day(time.time()))
        days, d, n = [today.isoformat()], today, int(today.weekday() < 5)  # (today always, even a weekend)
        while n < 5:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                days.append(d.isoformat())
                n += 1
        ours = sum(self._ny_day(t) in days for t in self.day_trades.get(mode, []))
        theirs = getattr(self.live_broker, "day_trades", None) if mode == "live" and self.is_live else None
        return max(ours, theirs or 0)

    def day_trade_status(self) -> dict:
        s = self.settings
        applies = s.day_trade_guard and s.venue() == "alpaca" and self.portfolio.equity < self.PDT_EQUITY \
            and any(not s.is_crypto(x) for x in s.symbols)
        return {"applies": applies, "used": self.day_trades_used(), "limit": self.PDT_LIMIT, "bots": s.day_trades_for_bots}

    def day_trade_block(self, symbol: str, force: bool = False) -> str | None:
        """Why the bots won't sell ``symbol`` today (it would be a day trade past their share), or None."""
        s = self.settings
        if force or not s.day_trade_guard or s.is_crypto(symbol) or s.venue() != "alpaca":
            return None
        if self.portfolio.equity >= self.PDT_EQUITY:
            return None
        if self._bought_on.get(self.mode, {}).get(symbol) != self._ny_day(time.time()):
            return None  # bought on an earlier day: selling it isn't a day trade
        used = self.day_trades_used()
        if used < s.day_trades_for_bots:
            return None
        return (f"day-trade rule: {used} of {self.PDT_LIMIT} day trades used in 5 trading days (balance under "
                f"$25,000) - {symbol} was bought today, so it sells from the next trading day; the last one is "
                "kept for your own Close all / Emergency stop (an automatic stop waits too)")

    def short_block(self, symbol: str) -> str:
        """Why the bots won't sell ``symbol`` short: never - they only sell what they hold."""
        return "the bots never short - they only sell what they hold"

    def free_cash(self) -> float:
        """Cash the bots may spend (no shorting: a negative position would be a book error the safety monitor flags)."""
        p = self.portfolio
        return max(0.0, p.cash - sum(abs(x.market_value) for x in p.open_positions if x.qty < 0))

    # -- transfer fees: what it costs to move profit out ------------------------------
    def transfer_fee(self) -> float:
        """Estimated cost of one transfer out (USD): your setting, else the cheapest linked wallet's network."""
        if self.settings.cashout_fee_usd > 0:
            return self.settings.cashout_fee_usd
        fees = [w.fee_usd for w in self.wallets.wallets.values()] if hasattr(self, "wallets") else []
        return min(fees) if fees else 1.0

    def min_take(self) -> float:
        """The least a profit-taking sale must make (after trading fees) to be worth its share of a transfer.
        Capped at 0.1% of the balance: a fee is paid once per cash-out, so on a small account it must not stop
        the bots banking small wins (they add up, and get moved out together)."""
        return min(self.transfer_fee() * self.settings.transfer_fee_share, 0.001 * max(self.portfolio.equity, 0.0))

    def cashout_summary(self) -> dict:
        """Profit the bots have banked, and what's left after one transfer fee to move it out."""
        p = self.portfolio
        banked = sum(t.pnl for t in p.closed_trades)
        fee = self.transfer_fee()
        return {"banked": banked, "transfer_fee": fee, "take_home": max(0.0, banked - fee),
                "worth_it": banked >= 10 * fee, "min_take": self.min_take()}

    def load_linked_broker(self) -> bool:
        s = self.settings
        linked = True
        if (d := self._account_row("uphold_live")) is not None:
            s.uphold_token, s.live_venue = d["token"], "uphold"
        elif (d := self._account_row("kraken_live")) is not None:
            s.kraken_key, s.kraken_secret, s.live_venue = d["key"], d["secret"], "kraken"
        elif (d := self._account_row("alpaca_live")) is not None:
            s.alpaca_live_key, s.alpaca_live_secret, s.live_venue = d["key"], d["secret"], "alpaca"
        else:
            linked = False
        if linked:
            s.apply_venue_costs()
            self.broker_source = "app"
            if not s.live_blockers():
                self.live_broker = build_live_broker(s)
        # Kraken (linked in the app or set on the server): the coins you held before going live stay yours
        if hasattr(self.live_broker, "capture_baseline") and (b := self._account_row("kraken_baseline")) is not None:
            self.live_broker.baseline = b  # type: ignore[union-attr]
        return linked

    async def _link_uphold(self, token: str) -> dict:
        from .brokers.uphold import UpholdBroker
        token = token.strip().removeprefix("Bearer ").strip()
        if len(token) < 20:
            raise ValueError("Paste your whole Uphold access token (it's a long code).")
        broker = UpholdBroker(token, symbols=self.settings.symbols)
        acct = await broker.account()
        if acct is None:
            await broker.close()
            raise ValueError("Uphold didn't accept this token. Make a new one and copy it completely.")
        await broker.prepare()
        self._use_venue("uphold", {"token": token})
        await self._adopt_live_broker(broker)
        unsupported = [s for s in self.settings.symbols if broker.supports(s) is False]
        if unsupported:
            self.bus.publish("alert", Alert("warning", "Uphold can't trade " + ", ".join(unsupported)
                                            + " - those stay practice-only", "desk"))
        return acct

    async def link_broker(self, key: str, secret: str = "", venue: str = "alpaca") -> dict:
        """Check the real-money credentials work, then use them for live trading (trading only, never withdrawals)."""
        if self.is_live:
            raise ValueError("Switch back to practice money before changing the account.")
        if venue == "uphold":
            return await self._link_uphold(key or "")
        if venue not in ("alpaca", "kraken"):
            raise ValueError("Choose Alpaca, Kraken or Uphold.")
        key, secret = (key or "").strip(), (secret or "").strip()
        if not key or not secret:
            raise ValueError("Paste both keys.")
        if venue == "kraken":
            from .brokers.kraken import KrakenBroker
            broker: Broker = KrakenBroker(key, secret)
            try:
                acct = await broker.verify()  # type: ignore[attr-defined]  # refuses keys that can withdraw
            except Exception:
                await broker.close()
                raise
        else:
            from .brokers.alpaca import AlpacaBroker
            if key.upper().startswith("PK"):
                raise ValueError("These are practice (paper) keys. For real money, open your Alpaca *live* account "
                                 "(not Paper Trading) and make the keys there - they start with AK.")
            broker = AlpacaBroker(key, secret, paper=False)
            acct = await broker.account()
            if acct is None:
                await broker.close()
                raise ValueError("Alpaca didn't accept these keys. Check you copied both parts completely.")
        self._use_venue(venue, {"key": key, "secret": secret})  # Kraken's baseline is recorded when live starts
        await self._adopt_live_broker(broker)
        return acct

    async def unlink_broker(self) -> None:
        if self.is_live:
            raise ValueError("Switch back to practice money first.")
        for kind in self.LIVE_ROWS:
            self._save_account_row(kind, None)
        if self.live_broker is not None:
            await self.live_broker.close()
        self.live_broker = None
        self._clear_live_credentials()
        self.settings.live_venue = "auto"
        self.settings.apply_venue_costs()  # practice goes back to the normal fee
        self.broker_source = ""

    def owner_protected(self) -> bool:
        """True once real money is safe to control: a PIN is set (or an access token), or local only."""
        sec = getattr(self, "security", None)
        if self.settings.api_token or (sec and sec.owner_exists):
            return True
        return self.settings.host in ("127.0.0.1", "localhost", "::1")

    def broker_info(self) -> dict:
        venue = self.settings.venue()
        key = {"uphold": self.settings.uphold_token, "kraken": self.settings.kraken_key}.get(venue, self.settings.alpaca_live_key)
        return {"linked": bool(key), "source": getattr(self, "broker_source", "") or ("server" if key else ""),
                "venue": venue, "label": VENUES[venue],
                "can_withdraw": False,  # TradeZone never withdraws; Kraken keys that could are refused
                "key": f"…{key[-4:]}" if len(key) >= 8 else "", "ready": not self.settings.live_blockers(),
                "blockers": self.settings.live_blockers(),
                "practice_only": [s for s in self.settings.symbols
                                  if self.live_broker is not None and self.live_broker.supports(s) is False]}

    ALPACA_UNLINKED = ("Alpaca unlinked as you asked: the bots no longer trade it. Anything it holds stays in your "
                       "Alpaca account, untouched (nothing was sold or moved). You can link it again in Settings.")

    def _unlink_alpaca_once(self) -> None:
        """Alpaca was unlinked on request (once): its saved keys are forgotten. Linking it again later is kept."""
        if self.store.load("alpaca_unlinked"):
            return
        self.store.save("alpaca_unlinked", {"done": True})
        if self._account_row("alpaca_live") is None:
            return
        self._save_account_row("alpaca_live", None)
        if self.settings.live_venue == "alpaca":
            self.settings.live_venue = "auto"
        self.settings.alpaca_live_key = self.settings.alpaca_live_secret = ""
        if self.live_broker is not None and getattr(self.live_broker, "name", "") == "alpaca":
            self._drop_broker = self.live_broker  # (closed in start)
            self.live_broker = None
        self.bus.publish("alert", Alert("warning", self.ALPACA_UNLINKED, "desk"))

    def _sl7_tp15_once(self) -> None:
        """Stop-loss at most -7% and take-profit +15% after fees, for the trades already open too (asked for, once).
        A trade already more than 7% down is then sold by the loss floor at the next price check."""
        if self.store.load("sl7_tp15"):
            return
        self.store.save("sl7_tp15", {"done": True})
        s, n = self.settings, 0
        for book, live in ((self.paper_portfolio, False), (self.live_portfolio, True)):
            for p in (book.open_positions if book is not None else []):
                if p.qty <= 0 or not p.avg_price:
                    continue
                floor = p.avg_price * (1 - s.wide_stop_pct)
                if p.stop_price is None or p.stop_price < floor * 0.999:  # (looser than -7%: tightened to it)
                    p.stop_price = floor
                if p.take_profit is not None:  # (a runner has none: only its stop, already in profit, steps up)
                    tp = s.plan_pcts(p.symbol, s.quick(live))[1]
                    target = p.avg_price * (1 + tp) + s.round_trip_cost(p.symbol, p.avg_price)
                    if abs(p.take_profit / target - 1) > 0.005:  # (+15% after fees)
                        p.take_profit = target
                n += 1
        if n:
            self.bus.publish("alert", Alert("info", f"New rules on {n} open trade(s): stop-loss at most -7%, take-profit "
                                                    f"+{s.take_profit_pct:.0%} after fees. A trade already more than 7% down "
                                                    "is sold.", "desk"))

    def _tp6_once(self) -> None:
        """The +6% take-profit (replayed: beat +15% on both halves) for the trades already open too, once: only a
        target still on the old automatic +15% after fees moves (a target you set on a trade yourself stays)."""
        if self.store.load("tp6"):
            return
        self.store.save("tp6", {"done": True})
        s, n = self.settings, 0
        for book, live in ((self.paper_portfolio, False), (self.live_portfolio, True)):
            for p in (book.open_positions if book is not None else []):
                if p.qty <= 0 or not p.avg_price or p.take_profit is None:
                    continue
                cost = s.round_trip_cost(p.symbol, p.avg_price)
                old = p.avg_price * 1.15 + cost
                if abs(p.take_profit / old - 1) <= 0.005:  # (the old automatic +15%: not one set by hand)
                    p.take_profit = p.avg_price * (1 + s.plan_pcts(p.symbol, s.quick(live))[1]) + cost
                    n += 1
        if n:
            self.bus.publish("alert", Alert("info", f"Take-profit is now +6% after every fee (it beat +15% on replays): "
                                                    f"{n} open trade(s) moved to it.", "desk"))

    async def start(self) -> None:
        self._unlink_alpaca_once()
        if (old := self.__dict__.pop("_drop_broker", None)) is not None:
            with contextlib.suppress(Exception):
                await old.close()
        before = self.live_broker
        self.load_linked_broker()
        if before is not None and before is not self.live_broker:
            await before.close()  # replaced by the account saved in the app: don't leave its connection open
        self._tune_live_broker()
        await self._venue_feed()
        self.bots["grok"].load_saved()  # type: ignore[attr-defined]  # key saved from the dashboard, if any
        restored = await self.restore_state()
        self._sl7_tp15_once()
        self._tp6_once()
        self.bots["flipper"].load()  # type: ignore[attr-defined]  # the hours it learned are good / bad for flips
        self.bots["accountant"].load()  # type: ignore[attr-defined]  # today's totals so far
        self.bots["stacker"].load()  # type: ignore[attr-defined]  # the hold stack
        self.bots["targets"].load()  # type: ignore[attr-defined]  # each coin's learned targets
        self.bots["reviewer"].load()  # type: ignore[attr-defined]  # each trade finder's record
        saved = self.store.load("strategies")
        if saved and isinstance(saved.get("on"), list) and "TZ_STRATEGIES" not in os.environ:
            from .bots.quant import STRATEGIES
            self.settings.strategies = [k for k in saved["on"] if k in STRATEGIES]
            if not saved.get("mfi_offered"):  # (saved before MFI was tested in: added once; switch it off any time)
                if "mfi" not in self.settings.strategies:
                    self.settings.strategies.append("mfi")
                self.store.save("strategies", {"on": self.settings.strategies, "mfi_offered": True, "macd_killed": True})
            if not saved.get("macd_killed"):  # (MACD failed on unseen data: taken out once; switch it on any time)
                self.settings.strategies = [k for k in self.settings.strategies if k != "macd"]
                self.store.save("strategies", {"on": self.settings.strategies, "mfi_offered": True, "macd_killed": True})
        if (cap := self.store.load("live_cap")) is not None and "TZ_LIVE_MAX_CAPITAL" not in os.environ:
            if not cap.get("chosen_after_all_cash"):  # (a cap saved before "use all cash": cleared once, as asked)
                cap = {"usd": 0.0, "chosen_after_all_cash": True}
                self.store.save("live_cap", cap)
            self.settings.live_max_capital = float(cap.get("usd", 0.0))
        if (ns := self.store.load("never_sell")) is not None and "TZ_NEVER_SELL_AT_LOSS" not in os.environ:
            self.settings.never_sell_at_loss = bool(ns.get("on"))
        if (dt := self.store.load("day_trades")) is not None:
            self.day_trades = {k: list(v) for k, v in (dt.get("trades") or {}).items()} or self.day_trades
            self._bought_on = {k: dict(v) for k, v in (dt.get("bought") or {}).items()} or self._bought_on
        if (lf := self.store.load("loss_floor")) is not None and "TZ_MAX_LOSS_PCT" not in os.environ:
            if not lf.get("chosen_after_7"):  # (saved before "stop-loss at most -7%": set to -7% once, as asked)
                lf = {"pct": float(self.BEST["max_loss_pct"]), "chosen_after_7": True}  # type: ignore[arg-type]
                self.store.save("loss_floor", lf)
            self.settings.max_loss_pct = float(lf.get("pct", 0.0))
        if (lc := self.store.load("loser_cap")) is not None and "TZ_LOSER_CAP" not in os.environ:
            if not lc.get("chosen_after_off"):  # (saved before it was switched off: switched off once, as asked)
                lc = {"cap": 0.0, "chosen_after_off": True}
                self.store.save("loser_cap", lc)
            self.settings.loser_cap = float(lc.get("cap", 0.0))
        if (fg := self.store.load("falling_guard")) is not None and "TZ_FALLING_GUARD" not in os.environ:
            self.settings.falling_guard = str(fg.get("mode", "off"))
        finders = self.store.load("finders") or {}
        if finders and not finders.get("_killed_after_unseen"):  # (saved before: the finders that failed go off once)
            finders = {**finders, "surge_snipe": False, "flipper": False, "_killed_after_unseen": True}
            self.store.save("finders", finders)
        for k, on in finders.items():
            if k in self.FINDERS and f"TZ_{self.FINDERS[k][0].upper()}" not in os.environ:
                setattr(self.settings, self.FINDERS[k][0], bool(on))
        self.bots["fees"].load()  # type: ignore[attr-defined]  # the fees really paid at this venue
        if (md := self.store.load("memedesk")) and md.get("url") and not hasattr(self, "account_id"):
            with contextlib.suppress(ValueError):
                self.memedesk.set_url(md["url"])
        if self.bots["coach"].load():  # type: ignore[attr-defined]  # what the team learned before the restart
            self.bots["coach"].act(f"remembered {self.bots['coach'].learned_trades} past trades")  # type: ignore[attr-defined]
        if back := self._watch_held():
            self.bus.publish("alert", Alert("info", f"Still holding {', '.join(back)}: watching {'it' if len(back) == 1 else 'them'} "
                                            "again after the restart", "desk"))
        await self.feed.start()
        if self.live_broker is not None:
            await self.live_broker.prepare()  # which coins the venue can trade (others stay practice-only)
        if dropped := getattr(self.feed, "dropped", None):
            self.bus.publish("alert", Alert("warning", f"Not listed on {self.feed.name}, skipped: {', '.join(dropped)}", "desk"))
        if added := getattr(self.feed, "added", None):
            self.bus.publish("alert", Alert("info", f"Meme coins on {self.feed.name.title()} the bots can trade: "
                                            + ", ".join(added), "desk"))
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
        t_start = time.perf_counter()
        try:
            await self._step()
        finally:
            self.last_cycle_s = time.perf_counter() - t_start  # (the Speed Monitor reads this)

    async def _step(self) -> None:
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
        if ticks:
            self._last_tick_ts = max(t.ts for t in ticks)
        await self.bus.drain()
        for bot in self.bots.values():
            if not bot.enabled:
                continue
            t0 = time.perf_counter()
            try:
                await bot.on_cycle()
            except Exception as exc:
                bot.errors += 1
                bot.act(f"cycle error: {exc}", "critical")
                log.exception("bot %s cycle failed", bot.name)
            bot.busy_s += time.perf_counter() - t0
            await self.bus.drain()
        self.cycles += 1
        if self.live_broker is not None and self.cycles % 30 == 1:  # bid / ask / volume for the "can it be sold?" check
            await self.live_broker.refresh_market()
        if self.cycles == 1 or self.cycles % 30 == 0:  # keep the status page fresh
            self.bots["scout"].metrics["feed_mode"] = getattr(self.feed, "mode", "poll")
        if self._dirty or self.cycles % 10 == 0:
            self.save_state()
        if not hasattr(self, "account_id") and (self.fleet.desks or self.live_broker is not None):
            if self.memedesk.due() and (self.__dict__.get("_meme_task") is None or self._meme_task.done()):
                self._meme_task = asyncio.ensure_future(self.memedesk.refresh())  # (the MemeCoin desk: in the background)
            self.fleet.refresh_soon()  # each exchange's own balance, once a minute (in the background: a slow
            # exchange must never hold up the trading cycle)

    # -- "today's hunt": what the bots looked at and why they did or didn't trade --------
    def _new_hunt(self) -> dict:
        return {"day": self._today(), "checks": 0, "agreed": 0, "snipes": 0, "skims": 0, "opened": 0, "closed": 0,
                "wins": 0, "skipped": {}}

    def _count_hunt(self, ev: Event) -> None:
        h = self.hunt
        if h["day"] != self._today():
            h = self.hunt = self._new_hunt()
        p = ev.payload
        if ev.topic == "market.snapshot":
            if not p.historical:
                h["checks"] += 1
        elif ev.topic == "signal.proposed":
            if p.source in ("quant", "skimmer") and p.kind.value == "entry" and self.portfolio.position(p.symbol).is_flat:
                h["agreed"] += 1
                h["snipes"] += p.reason.startswith(("snipe", "volume snipe"))
                h["skims"] += p.source == "skimmer"
        elif ev.topic in ("signal.rejected", "order.held"):
            src = p.get("signal") if ev.topic == "signal.rejected" else p.get("request")
            sym = src.get("symbol") if isinstance(src, dict) else getattr(src, "symbol", None)
            if sym and not self.portfolio.position(sym).is_flat:
                return  # an exit that waited, not a missed chance
            key = re.sub(r"[-+]?\$?[\d][\d,.]*%?|\(.*?\)", "", str(p.get("reason", ""))).strip()[:80]
            sk = h["skipped"]
            sk[key] = sk.get(key, 0) + 1
            if len(sk) > 60:  # keep the common reasons only
                for k in sorted(sk, key=sk.get)[:20]:
                    sk.pop(k, None)
        elif ev.topic == "trade.log":
            if p["action"] in ("open", "flip"):
                h["opened"] += 1
            elif p.get("pnl") is not None and abs(p.get("position_after") or 0) < 1e-12:
                h["closed"] += 1
                h["wins"] += p["pnl"] > 0

    # -- the goal: e.g. $50 -> $10,000, reached by compounding (never by taking more risk) ------
    RISK_WORD = "RISK"

    # -- which trading strategies the Quant runs (and the trade finders beside it) ----------------------
    FINDERS: ClassVar[dict[str, tuple[str, str, str]]] = {"snipe": ("snipe", "Meme breakout snipes", "trade finders"),
               "surge_snipe": ("surge_snipe", "Volume snipes", "trade finders"),
               "pullback": ("pullback", "Pullback Hunter", "trade finders"),
               "flipper": ("flipper", "Flipper (volume flips, best hours)", "trade finders")}

    def strategy_info(self) -> list[dict]:
        from .bots.coach import CORE, FAMILY_OF, NICE
        from .bots.quant import STRATEGIES
        coach = self.bots["coach"]
        fam = {"momentum": "trend-following", "mean_reversion": "buying dips", "breakout": "breakouts"}
        out = []
        for k in STRATEGIES:
            st = coach.stats["strategy"].get(k) or {"n": 0.0, "wins": 0.0, "pnl": 0.0}  # type: ignore[attr-defined]
            out.append({"key": k, "name": NICE[k], "core": k in CORE, "on": k in CORE or k in self.settings.strategies,
                        "family": fam[FAMILY_OF[k]], "trades": round(st["n"], 1), "pnl": round(st["pnl"], 2),
                        "trust": round(coach.strategy_mult(k), 2)})  # type: ignore[attr-defined]
        for k, (attr, name, family) in self.FINDERS.items():
            out.append({"key": k, "name": name, "core": False, "on": bool(getattr(self.settings, attr)),
                        "family": family, "trades": 0, "pnl": 0.0, "trust": 1.0})
        return out

    def set_strategy(self, key: str, on: bool) -> list[dict]:
        from .bots.coach import CORE
        from .bots.quant import STRATEGIES
        if key in self.FINDERS:
            setattr(self.settings, self.FINDERS[key][0], bool(on))
            self.store.save("finders", {**{k: bool(getattr(self.settings, a)) for k, (a, _, _) in self.FINDERS.items()}, "_killed_after_unseen": True})
            self.bus.publish("alert", Alert("info", f"{self.FINDERS[key][1]} {'on' if on else 'off'}", "operator"))
            return self.strategy_info()
        if key not in STRATEGIES:
            raise ValueError("unknown strategy")
        if key in CORE:
            raise ValueError("momentum, buying dips and breakouts are the core of the team and always run")
        chosen = [k for k in self.settings.strategies if k != key] + ([key] if on else [])
        self.settings.strategies = [k for k in STRATEGIES if k in chosen]
        self.store.save("strategies", {"on": self.settings.strategies, "mfi_offered": True, "macd_killed": True})
        self.bus.publish("alert", Alert("info", f"Strategy {key} {'on' if on else 'off'}", "operator"))
        return self.strategy_info()

    # -- "make me money" protocol: the safest proven path, step by step -------------------------
    #: the combination that tested best together on real price history (each value tested both ways)
    BEST: ClassVar[dict[str, object]] = {
        "strategies": ["obv", "mfi"], "snipe": True, "surge_snipe": False, "surge_volume_ratio": 4.0,
        "pullback": True, "flipper": False, "accumulate": False, "flip_bank_fraction": 0.0, "flip_max_skims": 0,
        "maker_exits": False, "fee_rule_stops": True, "never_sell_at_loss": True, "dip_buy": False,
        "learn_targets": True, "learn_targets_raise_only": True, "learn_sources": False, "full_cash": True,
        "profit_over_fees": 1.0, "reentry_bars": 5, "flip_min_strength": 0.22, "loser_cap": 0.0,
        "loser_cap_depth": 0.005, "wide_stop_pct": 0.07, "max_loss_pct": 0.07,
        # (replayed since: +6% after fees, runners, stale winners closed after 6 h, the turnaround rule, Pump Sniper)
        "take_profit_pct": 0.06, "major_take_profit_pct": 0.06, "tp_step_pct": 0.06, "runners": True,
        "max_hold_hours": 6.0, "meme_max_hold_hours": 3.0, "falling_coin_turn": True, "pump_sniper": True}

    def protocol_status(self) -> dict:
        s, now_ = self.settings, time.time()
        row = self.journal.one("SELECT MIN(ts) FROM equity WHERE mode = 'paper'")
        practice_days = (now_ - row[0]) / 86_400 if row and row[0] else 0.0
        practice_pnl = self.journal.last_pnl("paper") if not self.is_live else None
        if practice_pnl is None and not self.is_live:
            practice_pnl = self.lifetime_pnl()
        off = [{"key": k, "now": getattr(s, k), "best": v, "server": f"TZ_{k.upper()}" in os.environ}
               for k, v in self.BEST.items() if getattr(s, k) != v]
        week = None
        if self.live_portfolio is not None:
            p = self.live_portfolio
            recent = [t for t in p.closed_trades if t.ts >= now_ - 7 * 86_400]
            old = self.journal.one("SELECT pnl FROM equity WHERE mode = 'live' AND ts <= ? AND pnl IS NOT NULL "
                                   "ORDER BY ts DESC LIMIT 1", (now_ - 7 * 86_400,))
            held = [x for x in p.open_positions if s.net_if_sold(x, x.last_price or x.avg_price) < 0]
            week = {"closed": len(recent), "won": sum(t.pnl > 0 for t in recent), "realized": sum(t.pnl for t in recent),
                    "change": (self.lifetime_pnl() - old[0]) if (old and self.is_live) else None,
                    "held_losers": len(held), "held_loss": sum(s.net_if_sold(x, x.last_price or x.avg_price) for x in held)}
        steps = [
            {"key": "practice", "title": "Practise first", "done": practice_days >= 7 or self.is_live,
             "text": f"{practice_days:.1f} days on practice money" + (f", {practice_pnl:+,.2f}" if practice_pnl is not None else "")
                     + " - a week or two shows how the bots do on today's market"},
            {"key": "link", "title": "Link your exchange", "done": self.live_broker is not None,
             "text": f"{self.live_broker.label} linked (trading only - the bots can never withdraw)" if self.live_broker
                     else "Money → Real-money account"},
            {"key": "small", "title": "Start real money small", "done": self.is_live and s.live_max_capital > 0,
             "text": (f"the bots may use up to ${s.live_max_capital:,.0f}" if s.live_max_capital > 0 else
                      "set a cap below, then add more only after a few good weeks")
                     + ("" if self.is_live else " · not on real money yet")},
            {"key": "best", "title": "Use the tested-best settings", "done": not off,
             "text": "all on the best-tested combination" if not off else f"{len(off)} setting(s) differ from it"},
            {"key": "review", "title": "Review each week", "done": week is not None and week["closed"] > 0,
             "text": "after a week on real money, compare it with practice" if week is None else
                     f"last 7 days: {week['won']}/{week['closed']} closed trades won, {week['realized']:+,.2f} banked"
                     + (f", {week['held_losers']} losing trade(s) held ({week['held_loss']:+,.2f})" if week["held_losers"] else "")},
            {"key": "cashout", "title": "Take profits out now and then", "done": False,
             "text": "profit is only really yours once it's out of the exchange - the bots never withdraw; you do"},
        ]
        return {"steps": steps, "off": off, "week": week, "cap": s.live_max_capital, "risk_profile": s.risk_profile}

    def apply_best(self) -> dict:
        """Put every bot setting back on the combination that tested best (your risk level and cap stay yours)."""
        for k, v in self.BEST.items():
            setattr(self.settings, k, list(v) if isinstance(v, list) else v)
        self.store.save("strategies", {"on": self.settings.strategies, "mfi_offered": True, "macd_killed": True})
        self.store.save("finders", {**{k: bool(getattr(self.settings, a)) for k, (a, _, _) in self.FINDERS.items()}, "_killed_after_unseen": True})
        self.store.save("never_sell", {"on": self.settings.never_sell_at_loss})
        self.store.save("loser_cap", {"cap": self.settings.loser_cap, "chosen_after_off": True})
        self.store.save("loss_floor", {"pct": self.settings.max_loss_pct, "chosen_after_7": True})
        self.store.save("protocol", {"best": True, "ts": time.time()})
        self.bus.publish("alert", Alert("info", "Settings set to the tested-best combination", "operator"))
        return self.protocol_status()

    def set_live_cap(self, amount: float) -> dict:
        if amount < 0:
            raise ValueError("the cap can't be negative (0 = no cap)")
        self.settings.live_max_capital = float(amount)
        self.store.save("live_cap", {"usd": float(amount), "chosen_after_all_cash": True})
        self.bus.publish("alert", Alert("info", f"Real-money cap: {'$' + format(amount, ',.0f') if amount else 'none'}"
                                        " (takes effect the next time real money is switched on)", "operator"))
        return self.protocol_status()

    def set_never_sell(self, on: bool) -> dict:
        """Never sell at a loss (on) or cut losing trades with stops and early cuts (off)."""
        self.settings.never_sell_at_loss = bool(on)
        self.store.save("never_sell", {"on": bool(on)})
        self.bus.publish("alert", Alert("warning" if on else "info",
                                        "Never sell at a loss: " + ("ON - losing trades are held until they recover"
                                                                     if on else "OFF - stops and early cuts sell losers"),
                                        "operator"))
        return {"never_sell_at_loss": self.settings.never_sell_at_loss}

    def set_loss_floor(self, on: bool) -> dict:
        """The loss floor on (a trade 7% under what was paid is sold, even with never-sell on) or off."""
        self.settings.max_loss_pct = float(self.BEST["max_loss_pct"]) if on else 0.0  # type: ignore[arg-type]
        self.store.save("loss_floor", {"pct": self.settings.max_loss_pct, "chosen_after_7": True})
        self.bus.publish("alert", Alert("warning" if not on else "info", "Loss floor: " + (
            f"ON - a trade {self.settings.max_loss_pct:.0%} below what was paid is sold" if on
            else "OFF - with never-sell on, losing trades are held however far they fall"), "operator"))
        return {"max_loss_pct": self.settings.max_loss_pct}

    def set_falling_guard(self, on: bool) -> dict:
        """The falling-market guard on (no new buys of a coin in a downtrend) or off."""
        self.settings.falling_guard = "coin" if on else "off"
        self.store.save("falling_guard", {"mode": self.settings.falling_guard})
        self.bus.publish("alert", Alert("info", "Falling-market guard: " + (
            "ON - no new buys of a coin while it's below its 4-hour average and that average is falling"
            if on else "OFF - new buys whatever the trend"), "operator"))
        return {"falling_guard": self.settings.falling_guard}

    def set_loser_cap(self, on: bool) -> dict:
        """The underwater cap on (the tested 15%) or off (new buys whatever is held below break-even)."""
        self.settings.loser_cap = float(self.settings.loser_cap_on) if on else 0.0
        self.store.save("loser_cap", {"cap": self.settings.loser_cap, "chosen_after_off": True})
        self.bus.publish("alert", Alert("info", "Underwater cap: " + (
            f"ON - no new buys while trades held below break-even tie up {self.settings.loser_cap:.0%} of the balance"
            if on else "OFF - new buys whatever is held below break-even"), "operator"))
        return self.loser_cap_status()

    def loser_cap_status(self) -> dict:
        s, p = self.settings, self.portfolio
        stuck = sum(abs(x.qty * x.avg_price) for x in p.open_positions
                    if (px := self.prices.get(x.symbol)) and self._underwater(x, px))
        return {"cap": s.loser_cap, "stuck": stuck, "share": stuck / p.equity if p.equity > 0 else 0.0,
                "blocking": self.underwater_block() is not None}

    def set_risk_profile(self, profile: str, confirm: str = "") -> dict:
        """Normal or Aggressive (real money only). Turning Aggressive on needs the word RISK typed."""
        profile = (profile or "").strip().lower()
        if profile not in ("normal", "aggressive", "bold"):
            raise ValueError("choose normal, aggressive or bold")
        if profile != "normal" and confirm.strip().upper() != self.RISK_WORD:
            raise ValueError(f"type {self.RISK_WORD} to confirm bigger bets with real money")
        self.settings.risk_profile = profile
        self._dirty = True
        self.bus.publish("alert", Alert("warning" if profile != "normal" else "info",
                                        f"Real-money risk level: {profile.upper()}", "desk"))
        return self.risk_info(live=True)

    def risk_info(self, live: bool | None = None) -> dict:
        """The limits in force (for the current mode, or for real money with ``live=True``)."""
        live = self.is_live if live is None else live
        s = self.settings
        lim = s.limits(live)
        daily, dd, floor = s.live_limits() if live else (lim.max_daily_loss_pct, lim.max_drawdown_pct, None)
        return {"profile": s.risk_profile, "active": s.aggressive(live), "risk_per_trade": lim.risk_per_trade,
                "max_position_pct": lim.max_position_pct, "max_portfolio_heat": lim.max_portfolio_heat,
                "max_daily_loss_pct": min(lim.max_daily_loss_pct, daily), "max_drawdown_pct": min(lim.max_drawdown_pct, dd),
                "capital_floor_pct": floor}

    def daily_goal(self) -> float:
        # (the goal was removed: a daily goal saved earlier no longer changes how the bots trade)
        return float(self.settings.daily_goal_usd)

    def daily_goal_status(self) -> dict:
        """Today's profit vs the daily goal, and how big that goal is for the money on hand."""
        p, goal = self.portfolio, self.daily_goal()
        today = p.equity - p.day_start_equity
        need = goal / p.day_start_equity if p.day_start_equity > 0 else 0.0
        return {"goal": goal, "today": today, "progress": max(0.0, min(1.0, today / goal)) if goal > 0 else 0.0,
                "reached": goal > 0 and today >= goal, "pct_of_balance": need,
                "balance_for_1pct": goal * 100, "realistic": need <= 0.02}

    def set_daily_goal(self, amount: float) -> dict:
        if not (math.isfinite(amount) and amount >= 0):
            raise ValueError("the daily goal must be 0 (off) or more")
        self.goal["daily"] = float(amount)
        self._dirty = True
        return self.daily_goal_status()

    def set_goal(self, start: float, target: float) -> dict:
        if not (math.isfinite(start) and math.isfinite(target) and 0 < start < target <= start * 1e6):
            raise ValueError("the goal needs a starting amount above 0 and a bigger target")
        self.goal = {"start": float(start), "target": float(target), "bases": {}, "since": {},
                     **({"daily": self.goal["daily"]} if "daily" in self.goal else {})}
        self._dirty = True
        return self.goal_status()

    def goal_status(self) -> dict:
        s, g, eq = self.settings, self.goal, self.portfolio.equity
        start, target = g.get("start", s.goal_start), g.get("target", s.goal_target)
        bases, since = g.setdefault("bases", {}), g.setdefault("since", {})
        if self.mode not in bases and eq > 0:  # growth is measured from when the goal was set, per book
            bases[self.mode], since[self.mode] = eq, time.time()
            self._dirty = True
        growth = eq / bases.get(self.mode, eq) if eq > 0 else 0.0
        value = start * growth  # live with $50: your balance. Practice: what $50 would be now
        span = math.log(target / start)
        progress = max(0.0, min(1.0, math.log(max(value, 1e-9) / start) / span)) if value > 0 else 0.0
        days = max((time.time() - since.get(self.mode, time.time())) / 86_400, 0.0)
        eta = None
        if days >= 1 and growth > 1 and value < target:
            daily = growth ** (1 / days) - 1
            eta = math.log(target / value) / math.log(1 + daily)
        ladder = [m for m in (2, 5, 10, 20, 50, 100, 200, 500, 1000) if start * m <= target] + [target / start]
        nxt = next((start * m for m in ladder if start * m > value), target)
        left = math.log(target / value) if 0 < value < target else 0.0
        return {"start": start, "target": target, "value": round(value, 2), "growth": round(growth, 4),
                "progress": round(progress, 4), "next_milestone": round(nxt, 2), "days": round(days, 2),
                "eta_days": round(eta, 1) if eta is not None else None,
                "days_at_1pct": round(left / math.log(1.01)) if left else 0,
                "days_at_2pct": round(left / math.log(1.02)) if left else 0,
                "reached": value >= target}

    def hunt_summary(self) -> dict:
        h = self.hunt if self.hunt["day"] == self._today() else self._new_hunt()
        top = sorted(h["skipped"].items(), key=lambda kv: -kv[1])[:4]
        return {**{k: v for k, v in h.items() if k != "skipped"},
                "not_agreed": max(0, h["checks"] - h["agreed"]),
                "skipped": [{"reason": k, "count": v} for k, v in top]}

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
        r = self.pause_reason
        if self.paused and (r.startswith("switching to") or (r.startswith("execution integrity") and "slippage" in r)):
            self.paused, self.pause_reason = False, ""  # left over from an interrupted switch / an old bad-fill pause
        self.halted, self.halt_reason = data.get("halted", False), data.get("halt_reason", "")
        self.halt_kind = data.get("halt_kind", "")
        self.floor_ack = data.get("floor_ack", {})
        self.goal = data.get("goal", {})
        if data.get("risk_profile") in ("normal", "aggressive", "bold") and "TZ_RISK_PROFILE" not in os.environ:  # a server setting wins
            self.settings.risk_profile = data["risk_profile"]
        hunt = data.get("hunt")
        if isinstance(hunt, dict) and hunt.get("day") == self._today():
            self.hunt = {**self._new_hunt(), **hunt}
        if data.get("day") and data["day"] != self._day and self.halted and self.halt_kind == "daily" \
                and self.settings.auto_rearm_daily:
            self.halted, self.halt_reason, self.halt_kind = False, "", ""
        pm = self.bots["portfolio"]
        saved_pm = data.get("pm", {})
        self.bots["portfolio"].outside.update(saved_pm.get("outside", {}))  # type: ignore[attr-defined]
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
        pm.patient.update(s for s in saved_pm.get("patient", []) if s in active)  # type: ignore[attr-defined]
        pm.tp_steps.update({k: v for k, v in saved_pm.get("tp_steps", {}).items() if k in active})  # type: ignore[attr-defined]
        pm.watch.update({k: v for k, v in saved_pm.get("watch", {}).items() if k in active})  # type: ignore[attr-defined]
        self.bots["sentinel"].reset_shadow()  # type: ignore[attr-defined]
        return msg

    async def _can_resume_live(self, book: Portfolio) -> str | None:
        if self.settings.live_blockers() or self.live_broker is None:
            return "live trading is not configured"
        acct = await self.live_broker.account()
        if acct is None:
            return "live account unreachable"
        if await self.live_broker.positions() is None:
            return "couldn't read your live account's holdings"
        return None  # differences are sorted out by the records' check (yours vs the bots', sold outside, ...)

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

    START_RETRY_S = (10.0, 20.0, 40.0, 80.0, 120.0)

    async def run_forever(self) -> None:
        # A start that fails once (the exchange briefly unreachable while resuming real money, a key refused, ...)
        # used to end this team for good: no balance updates, no trades, and nothing on screen. Now it says why
        # and tries again, waiting a little longer each time (at most 2 minutes), until it starts or is stopped.
        attempt = 0
        self.running = True
        while self.running:
            try:
                await self.start()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = self.START_RETRY_S[min(attempt, len(self.START_RETRY_S) - 1)]
                attempt += 1
                who = getattr(self, "account_label", "") or "Main account"
                log.exception("%s: start failed (attempt %d)", who, attempt)
                self.start_error = f"{type(exc).__name__}: {exc}"[:200]
                self.bus.publish("alert", Alert("critical", f"{who} couldn't start ({self.start_error}) - trying again in "
                                                            f"{wait:.0f} s. No trades until it does.", "desk"))
                await asyncio.sleep(wait)
        if not self.running:
            return
        self.start_error = ""
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
        with contextlib.suppress(Exception):
            await self.memedesk.close()
        if (t := self.__dict__.get("_backfill_rest")) is not None and not t.done():
            t.cancel()  # (history still loading in the background)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.feed.close()
        await self.paper_broker.close()
        if self.live_broker is not None:
            await self.live_broker.close()
        await self.bots["grok"].close()  # type: ignore[attr-defined]
        await self.bots["news"].close()  # type: ignore[attr-defined]
        await self.bots["trends"].close()  # type: ignore[attr-defined]
        self.bots["coach"].save()  # type: ignore[attr-defined]
        self.bots["flipper"].save()  # type: ignore[attr-defined]
        self.bots["accountant"].save()  # type: ignore[attr-defined]
        self.bots["stacker"].save()  # type: ignore[attr-defined]
        self.bots["fees"].save()  # type: ignore[attr-defined]
        self.journal.commit()

    # -- views -----------------------------------------------------------
    def portfolio_view(self) -> dict:
        """The book for the screen: each open trade also says what selling at its stop would really leave
        after both fees (so "profit locked" is only claimed when it truly is)."""
        out = self.portfolio.to_dict()
        by = {p.symbol: p for p in self.portfolio.open_positions}
        for x in out["positions"]:
            p = by.get(x["symbol"])
            x["stop_net"] = self.settings.net_if_sold(p, p.stop_price) if p is not None and p.stop_price else None
            if p is not None and p.avg_price:  # (the trade's own plan, as the stop / target sheet shows and takes it)
                cost = self.settings.round_trip_cost(p.symbol, p.avg_price)
                x["stop_pct"] = 1 - p.stop_price / p.avg_price if p.stop_price else None
                x["target_pct"] = (p.take_profit - cost) / p.avg_price - 1 if p.take_profit else None  # (after fees)
        return out

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
            "portfolio": self.portfolio_view(),
            "pnl_point": [time.time(), self.lifetime_pnl()],
            "cashout": self.cashout_summary(),
            "focus": self.focus_status(),
            "limits": self.risk_info(),
            "live_risk": self.risk_info(live=True),  # the live chart follows this every update
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
            "coach": self.bots["coach"].summary(),  # type: ignore[attr-defined]
            "flipper": self.bots["flipper"].summary(),  # type: ignore[attr-defined]
            "trends": self.bots["trends"].summary(),  # type: ignore[attr-defined]
            "stack": self.bots["stacker"].summary(),  # type: ignore[attr-defined]
            "learned_targets": self.bots["targets"].summary(),  # type: ignore[attr-defined]
            "finders": self.bots["reviewer"].summary(),  # type: ignore[attr-defined]
            "strategies": self.strategy_info(),
            "never_sell_at_loss": self.settings.never_sell_at_loss,
            "loser_cap": self.loser_cap_status(),
            "max_loss_pct": self.settings.max_loss_pct,
            "falling_guard": self.settings.falling_guard,
            "day_trades": self.day_trade_status(),
            "protocol": self.protocol_status(),
            "held_losers": [{"symbol": p.symbol, "pnl": self.settings.net_if_sold(p, p.last_price or p.avg_price),
                             "pct": ((p.last_price or p.avg_price) / p.avg_price - 1) * (1 if p.qty > 0 else -1)}
                            for p in self.portfolio.open_positions
                            if self.settings.net_if_sold(p, p.last_price or p.avg_price) < 0],
            "daily_reports": self.bots["accountant"].summary(),  # type: ignore[attr-defined]
            "hunt": self.hunt_summary(),
            "accounts": self.fleet.summary() if not hasattr(self, "account_id") else [],
            "memedesk": self.memedesk.view() if not hasattr(self, "account_id") else None,
            "news": self.bots["news"].summary(),  # type: ignore[attr-defined]
            "goal": {**self.goal_status(), "guard": self.bots["risk"].day_guard(),  # type: ignore[attr-defined]
                     "daily": self.daily_goal_status()},
            "accuracy": {
                "execution": self.bots["execution"].accuracy,  # type: ignore[attr-defined]
                "fills_verified": self.bots["execution"].metrics.get("fills_verified", 0),
                "integrity_violations": self.bots["execution"].metrics.get("integrity_violations", 0),
                "books_balanced": sentinel.books_balanced,  # type: ignore[attr-defined]
                "audit_checks": sentinel.metrics.get("audit_checks", 0),
                "pause_reason": self.pause_reason,
            },
        }
