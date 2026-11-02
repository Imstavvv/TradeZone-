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

    def record_equity(self, ts: float, equity: float) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO equity(ts, equity) VALUES (?,?)", (ts, equity))

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
