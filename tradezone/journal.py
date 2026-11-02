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
CREATE TABLE IF NOT EXISTS equity (ts REAL NOT NULL, equity REAL NOT NULL);
"""


class Journal:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)

    def record(self, ts: float, topic: str, source: str, payload: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(ts, topic, source, payload) VALUES (?,?,?,?)",
                (ts, topic, source, json.dumps(payload, default=str)),
            )

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
