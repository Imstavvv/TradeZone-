"""The Trading Desk: wires market data, broker, event bus and the bot team."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

from .bots import TEAM
from .bots.base import Bot
from .brokers.base import Broker
from .config import VENUES, Settings
from .core.bus import Event, EventBus
from .core.models import Alert
from .core.portfolio import Portfolio
from .core.util import position_mismatches
from .journal import Journal
from .market.base import MarketFeed

log = logging.getLogger(__name__)


def build_feed(s: Settings) -> MarketFeed:
    if s.feed == "kraken":
        from .config import MEME_CANDIDATES
        from .market.kraken import KrakenFeed

        return KrakenFeed(s.symbols, meme_candidates=MEME_CANDIDATES if s.kraken_memes else [],
                          meme_coins=s.meme_coins, min_meme_volume_usd=s.min_meme_volume_usd,
                          max_memes=s.max_meme_coins)
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
            if hasattr(self.live_broker, "capture_baseline"):  # Kraken: never touch the coins you already had
                try:
                    baseline = await self.live_broker.capture_baseline()  # type: ignore[union-attr]
                except Exception as exc:
                    raise ModeSwitchError(f"couldn't read your {self.live_broker.label} balances: {exc}") from None
                self._save_account_row("kraken_baseline", baseline)
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
        if self.halt_kind == "floor":  # the owner chose to carry on from here: this is the new floor base
            self.floor_ack[self.mode] = self.portfolio.equity
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

    # -- transfer fees: what it costs to move profit out ------------------------------
    def transfer_fee(self) -> float:
        """Estimated cost of one transfer out (USD): your setting, else the cheapest linked wallet's network."""
        if self.settings.cashout_fee_usd > 0:
            return self.settings.cashout_fee_usd
        fees = [w.fee_usd for w in self.wallets.wallets.values()] if hasattr(self, "wallets") else []
        return min(fees) if fees else 1.0

    def min_take(self) -> float:
        """The least a profit-taking sale must make (after trading fees) to be worth its share of a transfer."""
        return self.transfer_fee() * self.settings.transfer_fee_share

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

    async def start(self) -> None:
        self.load_linked_broker()
        self._tune_live_broker()
        await self._venue_feed()
        self.bots["grok"].load_saved()  # type: ignore[attr-defined]  # key saved from the dashboard, if any
        restored = await self.restore_state()
        if self.bots["coach"].load():  # type: ignore[attr-defined]  # what the team learned before the restart
            self.bots["coach"].act(f"remembered {self.bots['coach'].learned_trades} past trades")  # type: ignore[attr-defined]
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
        if self.live_broker is not None and self.cycles % 30 == 1:  # bid / ask / volume for the "can it be sold?" check
            await self.live_broker.refresh_market()
        if self.cycles == 1 or self.cycles % 30 == 0:  # keep the status page fresh
            self.bots["scout"].metrics["feed_mode"] = getattr(self.feed, "mode", "poll")
        if self._dirty or self.cycles % 10 == 0:
            self.save_state()

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
                h["snipes"] += p.reason.startswith("snipe")
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
    def set_goal(self, start: float, target: float) -> dict:
        if not (math.isfinite(start) and math.isfinite(target) and 0 < start < target <= start * 1e6):
            raise ValueError("the goal needs a starting amount above 0 and a bigger target")
        self.goal = {"start": float(start), "target": float(target), "bases": {}, "since": {}}
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
        self.halted, self.halt_reason = data.get("halted", False), data.get("halt_reason", "")
        self.halt_kind = data.get("halt_kind", "")
        self.floor_ack = data.get("floor_ack", {})
        self.goal = data.get("goal", {})
        hunt = data.get("hunt")
        if isinstance(hunt, dict) and hunt.get("day") == self._today():
            self.hunt = {**self._new_hunt(), **hunt}
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
        diff = position_mismatches({p.symbol: p.qty for p in book.open_positions}, theirs)
        for sym, (a, b) in diff.items():
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
        await self.bots["news"].close()  # type: ignore[attr-defined]
        self.bots["coach"].save()  # type: ignore[attr-defined]
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
            "pnl_point": [time.time(), self.lifetime_pnl()],
            "cashout": self.cashout_summary(),  # the live chart follows this every update
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
            "hunt": self.hunt_summary(),
            "news": self.bots["news"].summary(),  # type: ignore[attr-defined]
            "goal": self.goal_status(),
            "accuracy": {
                "execution": self.bots["execution"].accuracy,  # type: ignore[attr-defined]
                "fills_verified": self.bots["execution"].metrics.get("fills_verified", 0),
                "integrity_violations": self.bots["execution"].metrics.get("integrity_violations", 0),
                "books_balanced": sentinel.books_balanced,  # type: ignore[attr-defined]
                "audit_checks": sentinel.metrics.get("audit_checks", 0),
                "pause_reason": self.pause_reason,
            },
        }
