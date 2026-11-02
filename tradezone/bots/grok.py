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


class GrokClient:
    def __init__(self, api_key: str, model: str, base_url: str, timeout: float,
                 client: httpx.AsyncClient | None = None) -> None:
        self.model = model
        self.client = client or httpx.AsyncClient(
            base_url=base_url, timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def complete(self, system: str, user: str) -> str:
        r = await self.client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

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
        s = desk.settings
        self.mode = s.grok_mode
        self.client = client
        if self.client is None and s.grok_api_key and self.mode != "off":
            self.client = GrokClient(s.grok_api_key, s.grok_model, s.grok_base_url, s.grok_timeout_s)
        self.connected: bool | None = None  # unknown until first call
        self.opinions: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        if self.client is None:
            self.mode = "off"
            self.last_action = "not connected (set TZ_GROK_API_KEY)"
        else:
            self.last_action = f"connected to {s.grok_model} in {self.mode} mode"

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
            self.connected = True
        except Exception as exc:
            self.bump("failures")
            if isinstance(exc, (httpx.HTTPError, KeyError)):
                self.connected = False
            log.warning("Grok review failed: %s", exc)
            ok = self.settings.grok_fail_open
            verdict = Verdict(ok, 0.0, f"Grok unavailable ({type(exc).__name__}); fail-{'open' if ok else 'closed'}")
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
        if self.client is None:
            return {"ok": False, "error": "Grok is not configured (set TZ_GROK_API_KEY)"}
        try:
            reply = await self.client.complete("Reply with the single word: pong", "ping")
            self.connected = True
            return {"ok": True, "model": self.client.model, "reply": reply[:100]}
        except Exception as exc:
            self.connected = False
            return {"ok": False, "error": str(exc)[:300]}

    async def on_cycle(self) -> None:
        self.metrics["mode"] = self.mode
        self.metrics["in_flight"] = len(self._tasks)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self.client:
            await self.client.close()
