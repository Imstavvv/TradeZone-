"""SQLite trade journal - a durable audit trail of everything the desk did."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, topic TEXT NOT NULL, source TEXT NOT NULL, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, ts);
CREATE INDEX IF NOT EXISTS idx_events_topic_id ON events(topic, id);
CREATE TABLE IF NOT EXISTS equity (ts REAL NOT NULL, equity REAL NOT NULL);
"""


class Journal:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "mode" not in cols:  # migrate older journals
            self.conn.execute("ALTER TABLE events ADD COLUMN mode TEXT")
        # equity points used to mix practice and real money: keep each book's points (and its P&L) apart
        ecols = {r[1] for r in self.conn.execute("PRAGMA table_info(equity)")}
        if "mode" not in ecols:
            self.conn.execute("ALTER TABLE equity ADD COLUMN mode TEXT")
        if "pnl" not in ecols:
            self.conn.execute("ALTER TABLE equity ADD COLUMN pnl REAL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_equity_mode_ts ON equity(mode, ts)")

    # -- small helpers so callers don't repeat lock/execute/commit ---------
    def run(self, sql: str, args: tuple = ()) -> int:
        """Execute one write and commit it. Returns the number of rows changed."""
        with self._lock:
            n = self.conn.execute(sql, args).rowcount
            self.conn.commit()
        return n

    def one(self, sql: str, args: tuple = ()) -> tuple | None:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def record(self, ts: float, topic: str, source: str, payload: Any, mode: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(ts, topic, source, payload, mode) VALUES (?,?,?,?,?)",
                (ts, topic, source, json.dumps(payload, default=str), mode),
            )

    def trades(self, mode: str | None = None, symbol: str | None = None,
               limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """The permanent trade log (every fill), newest first, with totals for the filter."""
        where, args = ["topic = 'trade.log'"], []
        if mode:
            where.append("mode = ?")
            args.append(mode)
        if symbol:
            where.append("json_extract(payload, '$.symbol') = ?")
            args.append(symbol)
        w = " WHERE " + " AND ".join(where)
        with self._lock:
            total, pnl, fees, wins, closes = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(json_extract(payload,'$.pnl')),0), "
                "COALESCE(SUM(json_extract(payload,'$.fee')),0), "
                "SUM(CASE WHEN json_extract(payload,'$.pnl') > 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN json_extract(payload,'$.pnl') IS NOT NULL THEN 1 ELSE 0 END) "
                f"FROM events{w}", args).fetchone()
            rows = self.conn.execute(
                f"SELECT payload FROM events{w} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset)
            ).fetchall()
        return {"total": total, "realized_pnl": pnl, "fees": fees, "closing_fills": closes or 0,
                "winning_fills": wins or 0, "items": [json.loads(r[0]) for r in rows]}

    def record_equity(self, ts: float, equity: float, mode: str | None = None, pnl: float | None = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO equity(ts, equity, mode, pnl) VALUES (?,?,?,?)", (ts, equity, mode, pnl))

    def last_pnl(self, mode: str) -> float | None:
        row = self.one("SELECT pnl FROM equity WHERE mode = ? AND pnl IS NOT NULL ORDER BY ts DESC LIMIT 1", (mode,))
        return row[0] if row else None

    def pnl_series(self, mode: str, since: float, until: float, points: int = 400) -> list[list[float]]:
        """Profit/loss over time for one book, thinned to about ``points`` points (the last of each slice)."""
        if since <= 0:
            row = self.one("SELECT MIN(ts) FROM equity WHERE mode = ? AND pnl IS NOT NULL", (mode,))
            since = row[0] if row and row[0] is not None else until
        bucket = max((until - since) / max(points, 1), 1.0)
        rows = self.all("SELECT MAX(ts), pnl FROM equity WHERE mode = ? AND ts >= ? AND pnl IS NOT NULL "
                        "GROUP BY CAST(ts / ? AS INTEGER) ORDER BY 1", (mode, since, bucket))
        return [[ts, pnl] for ts, pnl in rows]

    def trades_since(self, mode: str, since: float, limit: int = 300) -> list[dict[str, Any]]:
        """Fills of one book since ``since``, oldest first (for the chart's trade markers)."""
        rows = self.all("SELECT payload FROM events WHERE topic = 'trade.log' AND mode = ? AND ts >= ? "
                        "ORDER BY id DESC LIMIT ?", (mode, since, limit))
        keep = ("ts", "symbol", "side", "action", "qty", "price", "pnl", "fee")
        return [{k: d.get(k) for k in keep} for d in (json.loads(r[0]) for r in reversed(rows))]

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def query(self, topic: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT ts, topic, source, payload FROM events"
        args: tuple = ()
        if topic:
            sql += " WHERE topic = ?"
            args = (topic,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self.conn.execute(sql, (*args, limit)).fetchall()
        return [{"ts": r[0], "topic": r[1], "source": r[2], "payload": json.loads(r[3])} for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()
