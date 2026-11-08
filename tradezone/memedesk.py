"""The MemeCoin desk (github.com/Imstavvv/MemeCoinz-), attached read-only: TradeZone shows its balance, open positions
and record next to your accounts. It reads only the desk's public status (``GET /api/state``) - it can't trade on it
or change it, and the MemeCoin desk itself only paper-trades for now.

Only an https address on the public internet is accepted (never a private or local one), so the setting can't be used
to reach into your own network."""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


def check_url(url: str) -> str:
    """The desk's base address, cleaned up - or ValueError saying what's wrong with it."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/api/state"):
        u = u[: -len("/api/state")]
    p = urlparse(u)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Paste the desk's https:// address (for example https://memecoinz.onrender.com)")
    if p.username or p.password or p.query or p.fragment:
        raise ValueError("Just the desk's address, please (no password, ? or #)")
    host = p.hostname
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("That address is on your own network - TradeZone only reads a desk on the public internet")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        addrs = set()  # (can't resolve it from here right now: checked again on every read)
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            raise ValueError("That address is on a private or local network - TradeZone only reads a public desk")
    return u


class MemeDesk:
    REFRESH_S = 30.0

    def __init__(self) -> None:
        self.url = ""
        self.state: dict | None = None
        self.error = ""
        self.at = 0.0  # when it was last read successfully
        self.tried = 0.0
        self._client: httpx.AsyncClient | None = None

    def set_url(self, url: str) -> None:
        self.url = check_url(url) if url else ""
        self.state, self.error, self.at, self.tried = None, "", 0.0, 0.0

    def due(self) -> bool:
        return bool(self.url) and time.time() - self.tried >= self.REFRESH_S

    async def refresh(self) -> None:
        if not self.url:
            return
        self.tried = time.time()
        try:
            check_url(self.url)  # (its address could point somewhere private by now)
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=8.0, follow_redirects=False,
                                                 headers={"User-Agent": "TradeZone/1.0 (memecoin desk view)"})
            r = await self._client.get(self.url + "/api/state")
            r.raise_for_status()
            body = r.json()
            if not isinstance(body, dict) or "status" not in body:
                raise ValueError("that address didn't answer like a MemeCoin desk")
            self.state, self.error, self.at = body, "", time.time()
        except (httpx.HTTPError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:160]
            log.warning("memecoin desk read failed: %s", self.error)

    def view(self) -> dict | None:
        """What the Home card shows (None: no desk attached)."""
        if not self.url:
            return None
        s = self.state or {}
        positions = [{k: p.get(k) for k in ("symbol", "cost", "value", "opened_min_ago")} for p in (s.get("positions") or [])[:20]]
        return {"url": self.url, "status": s.get("status") or ("unreachable" if self.error else "connecting"),
                "mode": s.get("mode"), "equity": s.get("equity"), "pnl": s.get("pnl"), "cash": s.get("cash"),
                "kill_switch": bool(s.get("kill_switch")), "closed_trades": s.get("closed_trades"), "wins": s.get("wins"),
                "positions": positions, "recent": (s.get("recent_closed") or [])[:5], "desk_error": s.get("last_error"),
                "error": self.error, "age_s": round(time.time() - self.at) if self.at else None}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
