"""Grok - an AI analyst powered by xAI's Grok that second-guesses every new trade.

Modes (``TZ_GROK_MODE``):

* ``veto``     - every new-position signal must be approved by Grok before the Risk
                 Manager sees it. Exits are never delayed.
* ``advisory`` - Grok comments on signals but doesn't block them.
* ``off``      - disabled (the default when no API key is configured).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from ..core.bus import Event
from ..core.models import Alert, Side, Signal, SignalKind, now
from .base import Bot

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the senior risk-aware trading analyst on an automated trading desk.
A quantitative model has proposed opening a new position. Review the data and decide whether
the trade has a favourable risk/reward right now. Be skeptical: reject trades with weak or
conflicting evidence, trades against the prevailing regime, or chasing over-extended moves.
Respond ONLY with a JSON object of the form:
{"decision": "approve" | "reject", "confidence": <number 0..1>, "rationale": "<max 30 words>"}"""

MAX_IN_FLIGHT = 4


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@dataclass
class Verdict:
    approved: bool
    confidence: float
    rationale: str

    def to_dict(self) -> dict:
        return {"approved": self.approved, "confidence": self.confidence, "rationale": self.rationale}


def parse_verdict(text: str) -> Verdict:
    """Extract the JSON verdict from a model reply. Raises ValueError if malformed."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in Grok reply")
    data = json.loads(m.group(0))
    decision = str(data.get("decision", "")).strip().lower()
    if decision not in ("approve", "reject"):
        raise ValueError(f"unexpected decision {decision!r}")
    conf = float(data.get("confidence", 0.5))
    if not 0 <= conf <= 1:
        raise ValueError("confidence out of range")
    return Verdict(decision == "approve", conf, str(data.get("rationale", ""))[:300])


class GrokError(Exception):
    """A Grok failure explained in plain English (``kind``: auth, model, credits, rate, network, other)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _api_message(r: httpx.Response) -> str:
    try:
        j = r.json()
    except ValueError:
        return r.text[:200]
    err = j.get("error", j) if isinstance(j, dict) else j
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)[:200]
    return str(err)[:200]


def explain(r: httpx.Response, model: str) -> GrokError:
    msg = _api_message(r)
    low = msg.lower()
    if r.status_code in (401, 403) and ("credit" in low or "billing" in low or "spend" in low):
        return GrokError("credits", f"xAI says the account has no credits or spending limit left ({msg}). "
                                    "Add credits at console.x.ai.")
    if r.status_code in (401, 403):
        return GrokError("auth", f"xAI rejected the API key ({r.status_code}: {msg}). Check it's an active key "
                                 "from console.x.ai (they start with 'xai-').")
    if r.status_code in (400, 404) and "model" in low:
        return GrokError("model", f"Model '{model}' isn't available to this key ({msg}).")
    if r.status_code == 402:
        return GrokError("credits", f"xAI needs payment / credits ({msg}). Add credits at console.x.ai.")
    if r.status_code == 429:
        return GrokError("rate", f"xAI rate limit or quota reached ({msg}). It will retry automatically.")
    return GrokError("other", f"xAI error {r.status_code}: {msg}")


MODEL_PREFERENCE = ("grok-4", "grok-3", "grok")
MODEL_EXCLUDE = ("image", "vision", "imagine", "embed", "video", "audio", "tts")


def pick_model(ids: list[str], wanted: str = "") -> str | None:
    """Best chat model from the account's list: the configured one if present, else newest Grok."""
    if wanted and wanted in ids:
        return wanted
    chat = [i for i in ids if "grok" in i and not any(x in i for x in MODEL_EXCLUDE)]
    for pref in MODEL_PREFERENCE:
        cands = sorted([i for i in chat if i.startswith(pref)], key=lambda i: ("latest" not in i, "mini" in i, i))
        if cands:
            return cands[0]
    return None


class GrokClient:
    transport: httpx.AsyncBaseTransport | None = None  # tests inject a fake xAI here

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float,
                 client: httpx.AsyncClient | None = None) -> None:
        self.model = model
        self.client = client or httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=GrokClient.transport,
            headers={"Authorization": f"Bearer {api_key.strip()}"},  # pasted keys often carry spaces/newlines
        )

    async def complete(self, system: str, user: str) -> str:
        try:
            r = await self.client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                },
            )
        except httpx.TimeoutException:
            raise GrokError("network", "xAI didn't answer in time (timeout). It will retry automatically.") from None
        except httpx.HTTPError as exc:
            raise GrokError("network", f"Can't reach api.x.ai ({type(exc).__name__}). Check the server's internet "
                                       "access.") from None
        if r.status_code >= 400:
            raise explain(r, self.model)
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            raise GrokError("other", f"Unexpected reply from xAI: {r.text[:150]}") from None

    async def models(self) -> list[str]:
        try:
            r = await self.client.get("/models")
        except httpx.HTTPError as exc:
            raise GrokError("network", f"Can't reach api.x.ai ({type(exc).__name__})") from None
        if r.status_code >= 400:
            raise explain(r, self.model)
        data = r.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        return [m.get("id", "") if isinstance(m, dict) else str(m) for m in items]

    async def close(self) -> None:
        await self.client.aclose()


class GrokBot(Bot):
    name = "grok"
    title = "Grok"
    role = "AI Analyst (xAI Grok)"
    description = (
        "Sends each proposed trade, with its indicators, regime and portfolio context, to xAI's "
        "Grok for an independent second opinion, and vetoes trades Grok doesn't back."
    )

    def __init__(self, desk, client: GrokClient | None = None) -> None:
        super().__init__(desk)
        self.opinions: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self.last_error = ""
        self.key_source = ""  # env | dashboard
        self._key_hint = ""
        self._last_check = 0.0
        self.client: GrokClient | None = None
        self.configure(desk.settings.grok_api_key, desk.settings.grok_model, desk.settings.grok_mode, "env")
        if client is not None:  # injected (tests)
            self.client = client

    def configure(self, api_key: str, model: str, mode: str, source: str) -> None:
        """(Re)connect Grok with a key and model; used at start-up and from the dashboard."""
        s = self.desk.settings
        api_key = (api_key or "").strip()
        old = self.client
        self.client = GrokClient(api_key, model or s.grok_model, s.grok_base_url, s.grok_timeout_s) \
            if api_key and mode != "off" else None
        if old is not None:
            asyncio.ensure_future(old.close()) if _loop_running() else None
        self.mode = mode if self.client else "off"
        if self.mode == "auto":
            self.mode = "veto"
        self.key_source = source if api_key else ""
        self._key_hint = f"…{api_key[-4:]}" if len(api_key) >= 8 else ("set" if api_key else "")
        self.connected = None
        self.last_error = ""
        if self.client is None:
            self.last_action = "not connected - add your xAI API key (Grok button)"
        else:
            self.last_action = f"connecting to {self.client.model} in {self.mode} mode…"

    def subscriptions(self):
        return {"signal.proposed": self.on_signal}

    def state(self) -> str:
        if self.enabled and self.mode != "off" and self.connected is False:
            return "error"
        return super().state()

    # -- gating --------------------------------------------------------
    def gates(self, sig: Signal) -> bool:
        """True if this signal must wait for Grok's approval."""
        if not (self.enabled and self.mode == "veto" and self.client is not None):
            return False
        if sig.kind is SignalKind.EXIT:
            return False
        pos = self.portfolio.position(sig.symbol)
        closing = (pos.qty > 0 and sig.side is Side.SELL) or (pos.qty < 0 and sig.side is Side.BUY)
        return not closing

    def on_signal(self, ev: Event) -> None:
        sig: Signal = ev.payload
        if self.mode == "off" or self.client is None:
            return
        if sig.kind is SignalKind.EXIT or not (self.gates(sig) or self.mode == "advisory"):
            return
        if len(self._tasks) >= MAX_IN_FLIGHT:
            self.bump("skipped_busy")
            if self.gates(sig):
                self._publish_verdict(sig, Verdict(False, 0.0, "Grok busy - review queue full"))
            return
        task = asyncio.get_running_loop().create_task(self._review(sig))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def build_prompt(self, sig: Signal) -> str:
        risk = self.desk.bots.get("risk")
        snap = risk.snapshots.get(sig.symbol) if risk else None
        strategist = self.desk.bots.get("strategist")
        regime = strategist.regimes.get(sig.symbol) if strategist else None
        quant = self.desk.bots.get("quant")
        hist = self.desk.bots["scout"].history(sig.symbol)[-30:]
        p = self.portfolio
        ctx = {
            "proposal": {"symbol": sig.symbol, "side": sig.side.value, "conviction": sig.strength,
                         "reason": sig.reason},
            "price": self.desk.prices.get(sig.symbol),
            "indicators": snap.indicators if snap else None,
            "regime": regime.to_dict() if regime else None,
            "strategy_scores": quant.scores.get(sig.symbol) if quant else None,
            "recent_prices_oldest_first": [round(x, 6) for x in hist],
            "portfolio": {"equity": round(p.equity, 2), "cash": round(p.cash, 2),
                          "open_positions": [x.symbol for x in p.open_positions],
                          "drawdown": round(p.drawdown, 4)},
        }
        return json.dumps(ctx, default=str)

    async def _review(self, sig: Signal) -> None:
        self.bump("reviews")
        try:
            reply = await self.client.complete(SYSTEM_PROMPT, self.build_prompt(sig))  # type: ignore[union-attr]
            verdict = parse_verdict(reply)
            self.connected, self.last_error = True, ""
        except Exception as exc:
            self.bump("failures")
            if isinstance(exc, (GrokError, httpx.HTTPError, KeyError)):
                self.connected = False
                self.last_error = str(exc)
            log.warning("Grok review failed: %s", exc)
            ok = self.settings.grok_fail_open
            verdict = Verdict(ok, 0.0, f"Grok unavailable ({type(exc).__name__}); fail-{'open' if ok else 'closed'}")
            if self.bot_alerted_ok():
                self.desk.bus.publish("alert", Alert("warning", f"Grok review failed: {exc}", self.name), self.name)
        age = now() - sig.ts
        if verdict.approved and age > self.settings.grok_max_signal_age_s:
            verdict = Verdict(False, verdict.confidence, f"signal went stale during review ({age:.0f}s)")
        self.opinions[sig.symbol] = {"side": sig.side.value, **verdict.to_dict(), "ts": now()}
        self.bump("approved" if verdict.approved else "vetoed")
        if self.gates(sig) or self.mode == "veto":
            self._publish_verdict(sig, verdict)
        else:
            self.publish("grok.opinion", {"signal": sig.to_dict(), **verdict.to_dict()})
        word = "APPROVED" if verdict.approved else "VETOED"
        self.act(f"{word} {sig.side.value} {sig.symbol} ({verdict.confidence:.0%}): {verdict.rationale}",
                 "info" if verdict.approved else "warning")

    def _publish_verdict(self, sig: Signal, v: Verdict) -> None:
        self.publish("signal.reviewed", {"signal": sig, **v.to_dict()})

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def ping(self) -> dict:
        """Check the connection; if the configured model isn't available, switch to the best one that is."""
        self._last_check = time.monotonic()
        if self.client is None:
            self.connected = None
            return {"ok": False, "error": "Grok isn't set up yet - add your xAI API key."}
        try:
            try:
                reply = await self.client.complete("Reply with the single word: pong", "ping")
            except GrokError as exc:
                if exc.kind != "model":
                    raise
                best = pick_model(await self.client.models(), self.client.model)
                if not best:
                    raise GrokError("model", f"{exc} No Grok chat model is available to this key.") from None
                old, self.client.model = self.client.model, best
                self.act(f"model '{old}' unavailable - switched to '{best}'", "warning")
                reply = await self.client.complete("Reply with the single word: pong", "ping")
            was = self.connected
            self.connected, self.last_error = True, ""
            self.last_action = f"connected to {self.client.model} ({self.mode} mode)"
            if was is False:
                self.publish("alert", Alert("info", f"Grok reconnected ({self.client.model})", self.name))
            return {"ok": True, "model": self.client.model, "reply": reply[:100]}
        except GrokError as exc:
            was = self.connected
            self.connected, self.last_error = False, str(exc)
            self.last_action = f"NOT connected: {exc}"
            if was is not False:
                hold = " New trades are on hold until it works (or set Grok to advisory/off)." \
                    if self.mode == "veto" and not self.settings.grok_fail_open else ""
                self.publish("alert", Alert("critical", f"Grok can't connect: {exc}{hold}", self.name))
            return {"ok": False, "error": str(exc), "kind": exc.kind}

    def status(self):  # type: ignore[override]
        st = super().status()
        st.metrics = {**st.metrics, "mode": self.mode,
                      "model": self.client.model if self.client else "-"}
        return st

    def bot_alerted_ok(self) -> bool:
        """Alert on a failed review only once per outage (the health check owns outage alerts)."""
        first = not getattr(self, "_outage_alerted", False)
        self._outage_alerted = not self.connected
        return first and self.connected is not False

    # ---- dashboard-managed settings (stored server-side, never sent back to the browser) ----
    def _table(self):
        j = self.desk.journal
        with j._lock:
            j.conn.execute("CREATE TABLE IF NOT EXISTS grok_config (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT)")
            j.conn.commit()
        return j

    def load_saved(self) -> bool:
        j = self._table()
        with j._lock:
            row = j.conn.execute("SELECT data FROM grok_config WHERE id = 1").fetchone()
        if not row:
            return False
        d = json.loads(row[0])
        self.configure(d.get("api_key", ""), d.get("model", ""), d.get("mode", "veto"), "dashboard")
        return True

    def save(self, api_key: str | None, model: str | None, mode: str | None) -> None:
        j = self._table()
        with j._lock:
            row = j.conn.execute("SELECT data FROM grok_config WHERE id = 1").fetchone()
        cur = json.loads(row[0]) if row else {"api_key": self.settings.grok_api_key,
                                              "model": self.client.model if self.client else "", "mode": self.mode}
        if api_key:
            cur["api_key"] = api_key.strip()
        if model is not None:
            cur["model"] = model.strip()
        if mode:
            cur["mode"] = mode
        if cur.get("mode") == "off":
            cur["mode"] = "off"
        with j._lock:
            j.conn.execute("INSERT OR REPLACE INTO grok_config(id, data) VALUES (1, ?)", (json.dumps(cur),))
            j.conn.commit()
        self.configure(cur.get("api_key", ""), cur.get("model", ""), cur.get("mode") or "veto", "dashboard")

    def forget(self) -> None:
        j = self._table()
        with j._lock:
            j.conn.execute("DELETE FROM grok_config")
            j.conn.commit()
        s = self.settings
        self.configure(s.grok_api_key, s.grok_model, s.grok_mode, "env")

    def info(self) -> dict:
        return {"configured": self.client is not None, "mode": self.mode, "connected": self.connected,
                "model": self.client.model if self.client else self.settings.grok_model,
                "key": self._key_hint, "source": self.key_source, "error": self.last_error,
                "fail_open": self.settings.grok_fail_open}

    async def on_cycle(self) -> None:
        self.metrics["in_flight"] = len(self._tasks)
        # health check: at start-up, then every minute while disconnected, every 10 minutes otherwise
        if self.client is not None and self.enabled:
            every = 10 if self.connected is None else (60 if self.connected is False else 600)
            if time.monotonic() - self._last_check > every:
                self._last_check = time.monotonic()
                task = asyncio.get_running_loop().create_task(self.ping())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self.client:
            await self.client.close()
