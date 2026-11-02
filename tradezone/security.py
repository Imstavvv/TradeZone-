"""Site security: firewall, rate limiting, brute-force lockout, sessions, headers, audit log.

Layers, outermost first (``SecurityMiddleware`` wraps the whole app, HTTP and WebSocket):

1. **Firewall:** optional IP allowlist (``TZ_ALLOWED_IPS``, CIDRs) plus automatic bans.
2. **Rate limits:** requests per IP per minute, WebSocket connections per IP, max body size.
3. **Origin check:** state-changing requests and WebSockets from another site are refused
   (blocks cross-site request forgery and cross-site WebSocket hijacking).
4. **Authentication:** the access token is exchanged once for an HttpOnly, Secure,
   SameSite=Strict session cookie. Scripts can't read it and it never appears in a URL.
   API clients may still send ``Authorization: Bearer <token>``.
5. **Brute-force lockout:** repeated wrong tokens ban the IP, with escalating ban length.
6. **Step-up:** going live, adding a withdrawal wallet and withdrawing need the token again.
7. **Headers:** CSP with a per-request nonce, no framing, HSTS, nosniff, no-referrer and more.
8. **Audit log:** logins, failures, bans and sensitive actions, with IP and device.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

SESSION_COOKIE = "tz_session"
SESSION_TTL_S = 30 * 24 * 3600  # sliding: refreshed on use
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY, created REAL, last_seen REAL, expires REAL, ip TEXT, agent TEXT
);
CREATE TABLE IF NOT EXISTS bans (ip TEXT PRIMARY KEY, until REAL, strikes INTEGER, reason TEXT);
"""


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def is_local_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


@dataclass
class Window:
    """Fixed-window counter."""
    start: float = 0.0
    count: int = 0


@dataclass
class SecurityConfig:
    token: str = ""
    allowed_ips: list[str] = field(default_factory=list)
    requests_per_min: int = 600
    login_failures_before_ban: int = 5
    login_window_s: float = 900.0
    ban_s: float = 1800.0
    max_ban_s: float = 7 * 24 * 3600.0
    max_body_bytes: int = 64 * 1024
    max_ws_per_ip: int = 6
    max_ws_total: int = 60
    lock_after_min: float = 5.0


class SecurityManager:
    def __init__(self, cfg: SecurityConfig, journal) -> None:
        self.cfg = cfg
        self.journal = journal
        self._lock = threading.Lock()
        self.networks = [ipaddress.ip_network(c.strip(), strict=False) for c in cfg.allowed_ips if c.strip()]
        self._rate: dict[str, Window] = {}
        self._fails: dict[str, deque[float]] = {}
        self.ws_open: dict[str, int] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=300)
        with journal._lock:
            journal.conn.executescript(SCHEMA)
            journal.conn.commit()
            rows = journal.conn.execute("SELECT ip, until, strikes, reason FROM bans").fetchall()
        self.bans: dict[str, dict[str, Any]] = {
            ip: {"until": until, "strikes": strikes, "reason": reason} for ip, until, strikes, reason in rows
        }
        from .applock import AppLock

        self.lock = AppLock(journal, cfg.lock_after_min)

    @staticmethod
    def sid_hash(sid: str) -> str:
        return _hash(sid)

    # ------------------------------------------------------------ audit
    def audit(self, kind: str, ip: str = "", detail: str = "", agent: str = "", level: str = "info") -> None:
        ev = {"ts": time.time(), "kind": kind, "ip": ip, "detail": detail, "agent": agent[:160], "level": level}
        self.events.append(ev)
        try:
            self.journal.record(ev["ts"], f"security.{kind}", "security", ev)
            self.journal.commit()
        except Exception:  # never let logging break a request
            pass

    # --------------------------------------------------------- firewall
    @property
    def auth_required(self) -> bool:
        return bool(self.cfg.token)

    def allowed(self, ip: str) -> bool:
        if not self.networks:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.networks)

    def banned_for(self, ip: str) -> float:
        b = self.bans.get(ip)
        if not b:
            return 0.0
        left = b["until"] - time.time()
        return left if left > 0 else 0.0

    def ban(self, ip: str, reason: str) -> float:
        with self._lock:
            strikes = self.bans.get(ip, {}).get("strikes", 0) + 1
            length = min(self.cfg.ban_s * 2 ** (strikes - 1), self.cfg.max_ban_s)
            self.bans[ip] = {"until": time.time() + length, "strikes": strikes, "reason": reason}
        with self.journal._lock:
            self.journal.conn.execute(
                "INSERT OR REPLACE INTO bans(ip, until, strikes, reason) VALUES (?,?,?,?)",
                (ip, time.time() + length, strikes, reason))
            self.journal.conn.commit()
        self.audit("ban", ip, f"{reason} - banned {length / 60:.0f} min (strike {strikes})", level="critical")
        return length

    def unban(self, ip: str) -> bool:
        existed = self.bans.pop(ip, None) is not None
        self._fails.pop(ip, None)
        with self.journal._lock:
            self.journal.conn.execute("DELETE FROM bans WHERE ip = ?", (ip,))
            self.journal.conn.commit()
        if existed:
            self.audit("unban", ip, "unbanned by operator", level="warning")
        return existed

    def rate_ok(self, ip: str) -> bool:
        now = time.time()
        w = self._rate.setdefault(ip, Window(now, 0))
        if now - w.start >= 60:
            w.start, w.count = now, 0
        w.count += 1
        if w.count == self.cfg.requests_per_min + 1:  # log once per window, not per request
            self.audit("rate_limited", ip, f"over {self.cfg.requests_per_min} requests/min", level="warning")
        if len(self._rate) > 10_000:  # bound memory under a flood of distinct IPs
            self._rate = {k: v for k, v in self._rate.items() if now - v.start < 60}
        return w.count <= self.cfg.requests_per_min

    # ----------------------------------------------------------- tokens
    def check_token(self, candidate: str | None) -> bool:
        return bool(self.cfg.token) and hmac.compare_digest((candidate or "").encode(), self.cfg.token.encode())

    def login_failed(self, ip: str, agent: str = "") -> None:
        now = time.time()
        dq = self._fails.setdefault(ip, deque())
        dq.append(now)
        while dq and now - dq[0] > self.cfg.login_window_s:
            dq.popleft()
        self.audit("login_failed", ip, f"wrong access token ({len(dq)}/{self.cfg.login_failures_before_ban})",
                   agent, level="warning")
        if len(dq) >= self.cfg.login_failures_before_ban:
            dq.clear()
            self.ban(ip, "too many wrong access tokens")

    # --------------------------------------------------------- sessions
    def create_session(self, ip: str, agent: str) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.journal._lock:
            self.journal.conn.execute(
                "INSERT INTO sessions(id_hash, created, last_seen, expires, ip, agent) VALUES (?,?,?,?,?,?)",
                (_hash(sid), now, now, now + SESSION_TTL_S, ip, agent[:200]))
            self.journal.conn.commit()
        self._fails.pop(ip, None)
        self.audit("login", ip, "signed in - new session", agent)
        return sid

    def session_valid(self, sid: str | None, ip: str = "") -> bool:
        if not sid:
            return False
        h, now = _hash(sid), time.time()
        with self.journal._lock:
            row = self.journal.conn.execute("SELECT expires, last_seen FROM sessions WHERE id_hash = ?", (h,)).fetchone()
            if not row or row[0] < now:
                return False
            if now - row[1] > 60:  # sliding expiry, written at most once a minute
                self.journal.conn.execute(
                    "UPDATE sessions SET last_seen = ?, expires = ?, ip = COALESCE(NULLIF(?, ''), ip) WHERE id_hash = ?",
                    (now, now + SESSION_TTL_S, ip, h))
                self.journal.conn.commit()
        return True

    def revoke(self, sid: str | None) -> None:
        if sid:
            with self.journal._lock:
                self.journal.conn.execute("DELETE FROM sessions WHERE id_hash = ?", (_hash(sid),))
                self.journal.conn.commit()

    def revoke_all(self, ip: str = "") -> int:
        with self.journal._lock:
            n = self.journal.conn.execute("DELETE FROM sessions").rowcount
            self.journal.conn.commit()
        self.audit("sessions_revoked", ip, f"signed out all {n} device(s)", level="warning")
        return n

    def sessions(self) -> list[dict[str, Any]]:
        with self.journal._lock:
            rows = self.journal.conn.execute(
                "SELECT id_hash, created, last_seen, ip, agent FROM sessions WHERE expires > ? ORDER BY last_seen DESC",
                (time.time(),)).fetchall()
        return [{"id": r[0][:10], "created": r[1], "last_seen": r[2], "ip": r[3], "agent": r[4]} for r in rows]

    def summary(self) -> dict[str, Any]:
        now = time.time()
        return {
            "auth_required": self.auth_required,
            "allowlist": [str(n) for n in self.networks],
            "bans": [{"ip": ip, "until": b["until"], "strikes": b["strikes"], "reason": b["reason"]}
                     for ip, b in self.bans.items() if b["until"] > now],
            "sessions": self.sessions(),
            "events": list(self.events)[-100:][::-1],
            "limits": {"requests_per_min": self.cfg.requests_per_min,
                       "login_failures_before_ban": self.cfg.login_failures_before_ban,
                       "ban_minutes": self.cfg.ban_s / 60, "max_ws_per_ip": self.cfg.max_ws_per_ip},
        }


# ====================================================================== ASGI
def _headers(scope) -> dict[str, str]:
    return {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}


def client_ip(scope) -> str:
    c = scope.get("client")
    return c[0] if c else "unknown"


def same_origin(scope) -> bool:
    """True when there is no Origin (non-browser client) or it matches the Host being served."""
    h = _headers(scope)
    origin = h.get("origin")
    if not origin or origin == "null":
        return origin is None
    return urlsplit(origin).netloc.lower() == h.get("host", "").lower()


def security_headers(scope, nonce: str | None = None) -> list[tuple[bytes, bytes]]:
    script = f"'nonce-{nonce}'" if nonce else "'self'"
    host = re.sub(r"[^A-Za-z0-9.:\-\[\]]", "", _headers(scope).get("host", ""))[:255]  # never echo junk
    ws_src = f"wss://{host} ws://{host}" if host else ""
    csp = (
        "default-src 'self'; "
        f"script-src {script}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        f"connect-src 'self' {ws_src}; "
        "font-src 'self'; manifest-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
    )
    hdrs = [
        (b"content-security-policy", csp.encode()),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
    ]
    if scope.get("scheme") == "https":
        hdrs.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    if scope.get("path", "").startswith("/api/"):
        hdrs.append((b"cache-control", b"no-store"))
    return hdrs


class SecurityMiddleware:
    """Firewall + rate limiting + origin checks + security headers, for HTTP and WebSocket."""

    def __init__(self, app, sec: SecurityManager) -> None:
        self.app = app
        self.sec = sec

    def _has_session(self, scope) -> bool:
        cookie = _headers(scope).get("cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return self.sec.session_valid(v)
        return False

    async def _reject_http(self, send, status: int, msg: str, scope, retry_after: float = 0) -> None:
        body = json.dumps({"detail": msg}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if retry_after:
            headers.append((b"retry-after", str(int(retry_after) + 1).encode()))
        await send({"type": "http.response.start", "status": status,
                    "headers": headers + security_headers(scope)})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        ip, path, sec = client_ip(scope), scope.get("path", ""), self.sec

        reason, status, retry = None, 403, 0.0
        if path != "/api/health":  # the platform health check must always work
            if not sec.allowed(ip):
                reason = "access from this network is not allowed"
            elif (left := sec.banned_for(ip)) > 0 and not self._has_session(scope):
                # bans stop sign-in attempts; devices already signed in (e.g. you, on the same
                # Wi-Fi as whoever is guessing) keep working
                reason, status, retry = "temporarily blocked after repeated failed sign-ins", 429, left
            elif kind == "http" and not sec.rate_ok(ip):
                reason, status, retry = "too many requests - slow down", 429, 60
        if reason is None and kind == "http":
            h = _headers(scope)
            if int(h.get("content-length") or 0) > sec.cfg.max_body_bytes:
                reason, status = "request too large", 413
            elif scope.get("method") in ("POST", "PUT", "PATCH", "DELETE") and not same_origin(scope):
                reason = "cross-site request refused"
                sec.audit("cross_site_blocked", ip, f"{scope.get('method')} {path} from {h.get('origin')}",
                          level="warning")
        if reason is None and kind == "websocket":
            if not same_origin(scope):
                reason = "cross-site websocket refused"
                sec.audit("cross_site_blocked", ip, f"websocket from {_headers(scope).get('origin')}",
                          level="warning")
            elif sec.ws_open.get(ip, 0) >= sec.cfg.max_ws_per_ip or \
                    sum(sec.ws_open.values()) >= sec.cfg.max_ws_total:
                reason = "too many live connections"

        if reason is not None:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 4403, "reason": reason[:120]})
                return
            return await self._reject_http(send, status, reason, scope, retry)

        if kind == "websocket":
            sec.ws_open[ip] = sec.ws_open.get(ip, 0) + 1
            try:
                return await self.app(scope, receive, send)
            finally:
                sec.ws_open[ip] -= 1
                if sec.ws_open[ip] <= 0:
                    sec.ws_open.pop(ip, None)

        # enforce the body limit even without a content-length header (chunked uploads)
        received = 0

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > sec.cfg.max_body_bytes:
                    raise ValueError("request body too large")
            return msg

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in security_headers(scope, scope.get("state", {}).get("csp_nonce"))
                         if k not in existing]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        return await self.app(scope, limited_receive, send_with_headers)
