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

    def round_trips(self, mode: str, symbol: str | None = None, since: float = 0.0,
                    held: dict[str, dict] | None = None, limit: int = 150, now: float | None = None) -> dict[str, Any]:
        """Every trade from buy to sale, built from the fills: what was bought, at what average price, how it
        was sold (in parts or at once), the result after all fees, how long it was held and why.

        ``held``: what the account holds now (symbol -> {qty, last_price, unrealized_pnl, net}); a trade the
        records still show open that isn't held any more was moved to the hold stack (that isn't a sale)."""
        import time as _t
        now = now or _t.time()
        known = held is not None  # (without it, a trade still open in the records is simply shown open)
        held = held or {}
        where, args = ["topic = 'trade.log'", "mode = ?"], [mode]
        if symbol:
            where.append("json_extract(payload, '$.symbol') = ?")
            args.append(symbol)
        with self._lock:
            rows = self.conn.execute(f"SELECT payload FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 20000",
                                     args).fetchall()
        fills = [json.loads(r[0]) for r in reversed(rows)]
        trips: list[dict] = []
        cur: dict[str, dict] = {}

        def start(f: dict, qty: float | None = None, fee: float | None = None) -> dict:
            q = abs(qty if qty is not None else float(f.get("qty") or 0))
            return {"symbol": f["symbol"], "mode": mode, "side": "long",  # (no shorting: every trade is a purchase)
                    "opened": float(f.get("ts") or 0), "closed": None, "legs": [],
                    "_in": [(q, float(f.get("price") or 0))], "_out": [], "pnl": 0.0,
                    "fees": float(fee if fee is not None else (f.get("fee") or 0)), "entry_reason": f.get("reason") or "",
                    "exit_reason": "", "adds": 0}

        def leg(t: dict, f: dict, kind: str) -> None:
            t["legs"].append({"ts": f.get("ts"), "kind": kind, "qty": f.get("qty"), "price": f.get("price"),
                              "pnl": f.get("pnl"), "reason": f.get("reason") or ""})

        def finish(t: dict, status: str) -> None:
            t["status"] = status
            trips.append(t)

        for f in fills:
            sym, act = f.get("symbol"), f.get("action")
            after = float(f.get("position_after") or 0)
            t = cur.get(sym)
            if t is None and act in ("partial", "reduce", "close"):
                # a sale with no buy before it: coins moved OUT of the hold stack (or taken over from your account),
                # then sold by the bots (the move isn't a fill). A normal trade, its entry worked out from each sale.
                t = cur[sym] = start(f, qty=0.0, fee=0.0)
                t["_in"], t["from_stack"] = [], True
                t["entry_reason"] = "moved out of the hold stack"
            if act == "open" or t is None:
                if t is not None:  # opened again while the records still showed it open (e.g. moved to the stack)
                    finish(t, "moved")
                t = cur[sym] = start(f)
                leg(t, f, "buy")
            elif act == "add":
                t["_in"].append((abs(float(f.get("qty") or 0)), float(f.get("price") or 0)))
                t["fees"] += float(f.get("fee") or 0)
                t["adds"] += 1
                leg(t, f, "add")
            else:  # partial / reduce / close: a sale
                q = abs(float(f.get("qty") or 0))
                if act == "flip":
                    q -= abs(after)
                t["_out"].append((q, float(f.get("price") or 0)))
                if t.get("from_stack") and q > 0:  # what those coins were worth to the trading book
                    t["_in"].append((q, float(f.get("price") or 0) - float(f.get("pnl") or 0) / q))
                t["pnl"] += float(f.get("pnl") or 0)
                t["fees"] += float(f.get("fee") or 0)
                t["exit_reason"] = f.get("reason") or ""
                leg(t, f, "sell")
                if act == "flip" or abs(after) < 1e-12:
                    t["closed"] = float(f.get("ts") or 0)
                    finish(t, "closed")
                    del cur[sym]
                    if act == "flip":  # the rest opens the other way
                        cur[sym] = start(f, qty=after, fee=0.0)
        for sym, t in cur.items():
            h = held.get(sym)
            if h and abs(float(h.get("qty") or 0)) > 1e-12:
                t.update(now_price=h.get("last_price"), unrealized=h.get("unrealized_pnl"), net_now=h.get("net"))
                finish(t, "open")
            else:
                finish(t, "moved" if known else "open")
        for t in trips:
            qin = sum(q for q, _ in t["_in"]) or 1e-12
            qout = sum(q for q, _ in t["_out"])
            cost = sum(q * p for q, p in t["_in"])
            t["qty"] = qin
            t["avg_entry"] = cost / qin
            t["avg_exit"] = sum(q * p for q, p in t["_out"]) / qout if qout > 0 else None
            t["sold_parts"] = len(t["_out"])
            t["pct"] = t["pnl"] / cost if cost > 0 and t["status"] == "closed" else None
            t["held_s"] = (t["closed"] or now) - t["opened"]
            t["last"] = t["closed"] or (t["legs"][-1]["ts"] if t["legs"] else t["opened"])
            del t["_in"], t["_out"]
        shown = sorted((t for t in trips if t["last"] >= since), key=lambda t: -t["last"])
        closed = [t for t in shown if t["status"] == "closed"]
        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] <= 0]
        gw, gl = sum(t["pnl"] for t in wins), sum(t["pnl"] for t in losses)
        stats = {"closed": len(closed), "wins": len(wins), "losses": len(losses),
                 "win_rate": len(wins) / len(closed) if closed else None, "net": gw + gl,
                 "banked_open": sum(t["pnl"] for t in shown if t["status"] != "closed"),
                 "avg_win": gw / len(wins) if wins else None, "avg_loss": gl / len(losses) if losses else None,
                 "best": max(closed, key=lambda t: t["pnl"])["pnl"] if closed else None,
                 "worst": min(closed, key=lambda t: t["pnl"])["pnl"] if closed else None,
                 "fees": sum(t["fees"] for t in shown),
                 "avg_hold_s": sum(t["held_s"] for t in closed) / len(closed) if closed else None,
                 "profit_factor": gw / -gl if gl < 0 else None,
                 "open": sum(t["status"] == "open" for t in shown)}
        return {"trips": shown[:limit], "stats": stats, "total": len(shown)}

    def track_record(self, mode: str, now: float | None = None) -> dict:
        """How long this book has traded, its biggest drop and its longest time under water - measured on the
        profit (deposits and withdrawals don't count as gains or drops), one point an hour over its whole history."""
        import time as _t
        rows = self.all("SELECT MAX(ts), equity, pnl FROM equity WHERE mode = ? AND pnl IS NOT NULL "
                        "GROUP BY CAST(ts / 3600 AS INTEGER) ORDER BY 1", (mode,))
        if len(rows) < 2:
            return {"days": 0.0, "max_drawdown": None, "longest_recovery_s": None, "underwater_s": 0.0, "points": len(rows)}
        t_end = now if now is not None else _t.time()
        peak_pnl, peak_base, peak_ts = None, 0.0, rows[0][0]
        max_dd, longest = 0.0, 0.0
        for ts, eq, pnl in rows:
            base = eq - pnl  # (what was put in, at that moment)
            if peak_pnl is None or pnl >= peak_pnl:
                if peak_pnl is not None:
                    longest = max(longest, ts - peak_ts)  # (back at a new high: that stretch under water ended)
                peak_pnl, peak_base, peak_ts = pnl, base, ts
            else:
                top = peak_base + peak_pnl
                if top > 0:
                    max_dd = max(max_dd, (peak_pnl - pnl) / top)
        under = t_end - peak_ts if rows[-1][2] < peak_pnl else 0.0
        return {"days": (rows[-1][0] - rows[0][0]) / 86_400, "max_drawdown": max_dd,
                "longest_recovery_s": max(longest, under), "underwater_s": under, "points": len(rows)}

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
