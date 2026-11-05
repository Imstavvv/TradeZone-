"""More than one real-money account at the same time.

The main desk keeps its one linked account (Settings → Linked accounts). Every *extra* account linked here
gets its own complete bot team - its own book, its own risk limits, its own kill switch, its own
practice / LIVE switch - and trades at the same time as the main desk. Each account only ever trades its
own balance, so a loss or a stop in one never touches another, and the balances are shown separately.

Each extra account keeps its keys and its records in its own database file next to the main one; the main
database only lists which accounts exist (no keys). Bots still never withdraw: the same trading-only checks
as the main account apply (a Kraken key that can withdraw is refused).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import secrets
import time
from typing import TYPE_CHECKING

from .config import VENUES
from .core.models import Alert
from .journal import Journal

if TYPE_CHECKING:
    from .desk import TradingDesk

log = logging.getLogger(__name__)

#: the prices each venue's team trades on (Uphold has no public feed of its own: Coinbase's are used)
VENUE_FEED = {"kraken": "kraken", "alpaca": "alpaca", "uphold": "coinbase"}
MAX_EXTRA = 5
BALANCE_EVERY_S = 60.0


class FleetError(ValueError):
    pass


class AccountFleet:
    def __init__(self, main: TradingDesk) -> None:
        self.main = main
        self.desks: dict[str, TradingDesk] = {}
        self.meta: dict[str, dict] = {}  # id -> {"id", "venue", "label", "added_at"}
        self.tasks: dict[str, asyncio.Task] = {}
        self.balances: dict[str, dict | None] = {}  # id -> the exchange's own balance (refreshed each minute)
        self.balance_at: dict[str, float] = {}
        self.loaded = False

    # -- storage: the main database lists the accounts; each account's keys live in its own database ------
    def _rows(self) -> list[dict]:
        self.main._accounts_table()
        rows = self.main.journal.all("SELECT kind, data FROM linked_accounts WHERE kind LIKE 'extra:%'")
        return [json.loads(d) for _, d in rows]

    def _db_path(self, acct_id: str) -> str:
        base = self.main.journal.path if getattr(self.main.journal, "path", ":memory:") != ":memory:" else ""
        if not base:
            return ":memory:"
        root, ext = os.path.splitext(base)
        return f"{root}-account-{acct_id}{ext or '.db'}"

    def _settings(self, venue: str, creds: dict | None):
        """A copy of the main settings for one account: same risk level, goals and bots, its own venue."""
        s = copy.deepcopy(self.main.settings)
        s.alpaca_live_key = s.alpaca_live_secret = s.kraken_key = s.kraken_secret = s.uphold_token = ""
        s.alpaca_key = s.alpaca_secret = ""  # (the main desk's practice account isn't this account's)
        s.broker, s.mode, s.live_venue = "paper", "paper", venue
        if self.main.settings.feed != "sim":  # (tests run everything on simulated prices)
            s.feed = s.feed_choice = VENUE_FEED[venue]
            s.symbols = []  # the venue's own default list
        if creds:
            if venue == "uphold":
                s.uphold_token = creds["token"]
            elif venue == "kraken":
                s.kraken_key, s.kraken_secret = creds["key"], creds["secret"]
            else:
                s.alpaca_live_key, s.alpaca_live_secret = creds["key"], creds["secret"]
        if hasattr(s, "_base_crypto_fee_bps"):
            s.crypto_fee_bps = s._base_crypto_fee_bps
        s.journal_path = ""
        return s

    def make_desk(self, settings, journal: Journal) -> TradingDesk:
        from .desk import TradingDesk
        feed = None
        if settings.feed == "sim":
            from .market.simulated import SimulatedFeed
            feed = SimulatedFeed(settings.symbols, seed=settings.seed)
        d = TradingDesk(settings, feed=feed, journal=journal)
        d.security = getattr(self.main, "security", None)  # type: ignore[attr-defined]  # same owner sign-in
        return d

    def _open(self, meta: dict) -> TradingDesk:
        journal = Journal(self._db_path(meta["id"]))
        _table(journal)
        row = journal.one("SELECT data FROM linked_accounts WHERE kind = ?", (f"{meta['venue']}_live",))
        creds = json.loads(row[0]) if row else None
        d = self.make_desk(self._settings(meta["venue"], creds), journal)
        d.account_id = meta["id"]  # type: ignore[attr-defined]
        d.account_label = meta["label"]  # type: ignore[attr-defined]
        return d

    # -- lifecycle ------------------------------------------------------------------
    def load(self) -> None:
        """Open every extra account that was linked before (they start once the server runs)."""
        if self.loaded:
            return
        self.loaded = True
        for meta in self._rows():
            try:
                self.desks[meta["id"]] = self._open(meta)
                self.meta[meta["id"]] = meta
            except Exception as exc:
                log.exception("could not open account %s", meta.get("id"))
                self.main.bus.publish("alert", Alert("critical", f"Couldn't open the linked {VENUES.get(meta.get('venue'), '?')} "
                                                                 f"account '{meta.get('label')}': {exc}", "fleet"))

    async def start(self) -> None:
        self.load()
        for acct_id in self.desks:
            self._run(acct_id)

    def _run(self, acct_id: str) -> None:
        if acct_id in self.tasks and not self.tasks[acct_id].done():
            return
        self.tasks[acct_id] = asyncio.create_task(self.desks[acct_id].run_forever(), name=f"account-{acct_id}")

    async def stop(self) -> None:
        for d in self.desks.values():
            d.stop()
        for t in self.tasks.values():
            t.cancel()
        for t in self.tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self.tasks.clear()
        for d in self.desks.values():  # a desk that never ran still has its records saved
            d.save_state()
            d.journal.commit()

    # -- linking --------------------------------------------------------------------
    def _keys_in_use(self) -> set[str]:
        out = set()
        for s in [self.main.settings, *(d.settings for d in self.desks.values())]:
            out |= {k for k in (s.kraken_key, s.alpaca_live_key, s.uphold_token) if k}
        return out

    async def add(self, venue: str, key: str, secret: str = "", label: str = "") -> dict:
        """Check the account (trading only, never withdrawals) and give it its own bot team."""
        if venue not in VENUES:
            raise FleetError("Choose Kraken, Alpaca or Uphold.")
        if len(self.desks) >= MAX_EXTRA:
            raise FleetError(f"Up to {MAX_EXTRA} extra accounts can trade at the same time.")
        k = (key or "").strip().removeprefix("Bearer ").strip()
        if k and k in self._keys_in_use():
            raise FleetError("That account is already linked - two teams trading one balance would trip over "
                             "each other. Make a separate account (or a separate sub-account) for each team.")
        self.load()
        acct_id = secrets.token_hex(4)
        meta = {"id": acct_id, "venue": venue, "label": (label or "").strip()[:40] or f"{VENUES[venue]} {len(self.desks) + 2}",
                "added_at": time.time()}
        journal = Journal(self._db_path(acct_id))
        creds = {"token": k} if venue == "uphold" else {"key": k, "secret": (secret or "").strip()}
        d = self.make_desk(self._settings(venue, creds), journal)  # (checked properly just below)
        d.account_id, d.account_label = acct_id, meta["label"]  # type: ignore[attr-defined]
        try:
            acct = await d.link_broker(key, secret, venue=venue)  # the main account's own checks
        except Exception:
            await _discard(d)
            raise
        self.desks[acct_id], self.meta[acct_id] = d, meta
        self.main._save_account_row(f"extra:{acct_id}", meta)
        self.balances[acct_id], self.balance_at[acct_id] = acct, time.time()
        if self.main.running:
            self._run(acct_id)
        self.main.bus.publish("alert", Alert("info", f"Linked another real-money account: {meta['label']} "
                                                     f"({VENUES[venue]}) - its own bot team, practice money until you switch it to LIVE",
                                             "fleet"))
        return {**self.row(acct_id), "account": acct}

    async def remove(self, acct_id: str) -> None:
        d = self._get(acct_id)
        if d.is_live:
            raise FleetError("Switch this account back to practice money first.")
        d.stop()
        if (t := self.tasks.pop(acct_id, None)) is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            await d.unlink_broker()  # forget its keys
        await _discard(d, delete=False)
        self.desks.pop(acct_id, None)
        self.meta.pop(acct_id, None)
        self.balances.pop(acct_id, None)
        self.main._save_account_row(f"extra:{acct_id}", None)

    def _get(self, acct_id: str) -> TradingDesk:
        if acct_id == "main":
            return self.main
        d = self.desks.get(acct_id)
        if d is None:
            raise KeyError(acct_id)
        return d

    def desk(self, acct_id: str) -> TradingDesk:
        return self._get(acct_id)

    # -- controls -------------------------------------------------------------------
    def control_all(self, action: str, reason: str = "operator") -> int:
        """pause / resume / kill every account at once (the main one too). Returns how many."""
        n = 0
        for d in [self.main, *self.desks.values()]:
            if action == "pause":
                d.pause(reason)
            elif action == "resume":
                d.resume()
            elif action == "kill":
                d.halt(f"manual kill (all accounts): {reason}", flatten=True)
            else:
                raise FleetError("action must be pause, resume or kill")
            n += 1
        return n

    # -- balances -------------------------------------------------------------------
    async def refresh_balances(self, force: bool = False) -> None:
        """Ask each exchange for its own balance (once a minute): shown next to what the bots count."""
        for acct_id, d in [("main", self.main), *self.desks.items()]:
            if d.live_broker is None:
                self.balances[acct_id] = None
                continue
            if not force and time.time() - self.balance_at.get(acct_id, 0) < BALANCE_EVERY_S:
                continue
            self.balance_at[acct_id] = time.time()
            try:
                self.balances[acct_id] = await asyncio.wait_for(d.live_broker.account(), 15)
            except Exception as exc:
                log.warning("balance of %s: %s", acct_id, exc)

    def row(self, acct_id: str) -> dict:
        d = self._get(acct_id)
        p = d.portfolio
        meta = self.meta.get(acct_id, {})
        venue = d.settings.venue() if d.live_broker is not None or acct_id != "main" else ""
        label = meta.get("label") or (f"Main · {VENUES[venue]}" if venue else "Main (practice only)")
        bal = self.balances.get(acct_id)
        return {
            "id": acct_id, "label": label, "venue": venue, "venue_label": VENUES.get(venue, ""),
            "main": acct_id == "main", "linked": d.live_broker is not None,
            "mode": "live" if d.is_live else "paper", "running": d.running,
            "cash": p.cash, "equity": p.equity, "pnl_today": p.equity - p.day_start_equity,
            "pnl_total": d.lifetime_pnl(), "open": len(p.open_positions),
            "positions": [{"symbol": x.symbol, "qty": x.qty, "value": x.market_value, "pnl": x.unrealized_pnl}
                          for x in p.open_positions][:20],
            "paused": d.paused, "pause_reason": d.pause_reason, "halted": d.halted, "halt_reason": d.halt_reason,
            "feed": d.feed.name, "feed_ok": not d.feed_down,
            "exchange": ({"cash": bal.get("cash"), "equity": bal.get("equity", bal.get("portfolio_value"))}
                         if isinstance(bal, dict) else None),
            "exchange_at": self.balance_at.get(acct_id),
        }

    def summary(self) -> list[dict]:
        rows = [self.row("main")]
        for acct_id in self.desks:
            try:
                rows.append(self.row(acct_id))
            except Exception:
                log.exception("account %s summary", acct_id)
        return rows

    def totals(self) -> dict:
        rows = self.summary()
        live = [r for r in rows if r["mode"] == "live"]
        return {"accounts": len(rows), "live": len(live), "live_equity": sum(r["equity"] for r in live),
                "live_pnl_today": sum(r["pnl_today"] for r in live)}


def _table(journal: Journal) -> None:
    with journal._lock:
        journal.conn.execute("CREATE TABLE IF NOT EXISTS linked_accounts (kind TEXT PRIMARY KEY, data TEXT)")
        journal.conn.commit()


async def _discard(d: TradingDesk, delete: bool = True) -> None:
    with contextlib.suppress(Exception):
        await d.feed.close()
    if d.live_broker is not None:
        with contextlib.suppress(Exception):
            await d.live_broker.close()
    for name in ("grok", "news", "trends"):
        with contextlib.suppress(Exception):
            await d.bots[name].close()  # type: ignore[attr-defined]
    path = getattr(d.journal, "path", ":memory:")
    with contextlib.suppress(Exception):
        d.journal.close()
    if delete and path != ":memory:":
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(path + suffix)
