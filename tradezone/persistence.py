"""Save and restore the desk's state so restarts (deploys, crashes) don't lose positions.

State is written to the journal database (a persistent disk on Render) after every fill and
control action, and at least every few cycles while running.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, fields
from typing import TYPE_CHECKING, Any

from .core.portfolio import ClosedTrade, Portfolio, Position

if TYPE_CHECKING:
    from .desk import TradingDesk

SCHEMA = "CREATE TABLE IF NOT EXISTS desk_state (key TEXT PRIMARY KEY, saved_at REAL, data TEXT NOT NULL);"
KEEP_TRADES = 500


def portfolio_to_state(p: Portfolio) -> dict[str, Any]:
    return {
        "starting_cash": p.starting_cash, "cash": p.cash, "commissions": p.commissions,
        "peak_equity": p.peak_equity, "day_start_equity": p.day_start_equity, "net_flows": p.net_flows,
        "positions": [asdict(x) for x in p.positions.values() if not x.is_flat],
        "closed_trades": [asdict(t) for t in p.closed_trades[-KEEP_TRADES:]],
    }


def portfolio_from_state(d: dict[str, Any]) -> Portfolio:
    p = Portfolio(d["starting_cash"])
    for k in ("cash", "commissions", "peak_equity", "day_start_equity", "net_flows"):
        setattr(p, k, d.get(k, getattr(p, k)))
    pos_fields = {f.name for f in fields(Position)}
    for raw in d.get("positions", []):
        pos = Position(**{k: v for k, v in raw.items() if k in pos_fields})
        p.positions[pos.symbol] = pos
    trade_fields = {f.name for f in fields(ClosedTrade)}
    p.closed_trades = [ClosedTrade(**{k: v for k, v in t.items() if k in trade_fields})
                       for t in d.get("closed_trades", [])]
    return p


class StateStore:
    def __init__(self, journal) -> None:
        self.journal = journal
        with journal._lock:
            journal.conn.executescript(SCHEMA)

    def save(self, key: str, data: dict) -> None:
        self.journal.run(
            "INSERT OR REPLACE INTO desk_state(key, saved_at, data) VALUES (?,?,?)",
            (key, time.time(), json.dumps(data, default=str)),
        )

    def load(self, key: str) -> dict | None:
        row = self.journal.one("SELECT data FROM desk_state WHERE key = ?", (key,))
        return json.loads(row[0]) if row else None


def snapshot(desk: TradingDesk) -> dict[str, Any]:
    pm = desk.bots["portfolio"]
    return {
        "version": 1,
        "mode": desk.mode,
        "paused": desk.paused, "pause_reason": desk.pause_reason,
        "halted": desk.halted, "halt_reason": desk.halt_reason, "halt_kind": desk.halt_kind,
        "floor_ack": desk.floor_ack, "goal": desk.goal,
        "day": desk._day,
        "paper": portfolio_to_state(desk.paper_portfolio),
        "live": portfolio_to_state(desk.live_portfolio) if desk.live_portfolio else None,
        "pm": {
            "trail_dist": pm.trail_dist,  # type: ignore[attr-defined]
            "protected": sorted(pm.protected),  # type: ignore[attr-defined]
            "scaled_out": sorted(pm.scaled_out),  # type: ignore[attr-defined]
        },
    }
