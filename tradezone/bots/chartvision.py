"""Chart Vision: xAI's Grok looks at the coin's chart - a real candle picture, the way a trader would.

When a buy is proposed, the coin's last 80 candles (1-hour by default) are drawn as a PNG and sent to Grok's vision
model, which answers with the trend (bullish / bearish / sideways), how sure it is, and the pattern it sees. The
read is kept for 30 minutes; buys use the latest one:

* bearish and fairly sure (70%+): a little less sure and smaller - or, with ``chart_vision = "veto"``, no buy;
* bullish and fairly sure: a little more sure.

Needs the Grok key (the same one as the Grok analyst). Each look is a paid xAI request, so at most
``chart_vision_per_hour`` an hour, and never more than two at a time. Sells are never touched.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque

from ..core.chart_png import candle_png
from ..core.models import Side
from .advisors import NEUTRAL, Advisor

SYSTEM = """You are a veteran chart technician. You are shown a candlestick chart (oldest candle on the left, the
newest on the right, volume bars underneath). Read the chart itself: trend structure (higher highs/lows or lower),
support and resistance, breakouts or breakdowns, patterns, and volume. Judge where price is most likely to go over
the next several candles. Respond ONLY with JSON:
{"trend": "bullish" | "bearish" | "sideways", "confidence": <0..1>, "pattern": "<max 6 words>", "reason": "<max 25 words>"}"""

FRESH_S = 1800.0
SURE = 0.7
MIN_CANDLES = 30


def parse_read(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON in the chart read")
    d = json.loads(m.group(0))
    trend = str(d.get("trend", "")).strip().lower()
    if trend not in ("bullish", "bearish", "sideways"):
        raise ValueError(f"unexpected trend {trend!r}")
    conf = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    return {"trend": trend, "confidence": round(conf, 2), "pattern": str(d.get("pattern", ""))[:60],
            "reason": str(d.get("reason", ""))[:200]}


class ChartVisionBot(Advisor):
    name = "chartvision"
    title = "Chart Vision"
    role = "Chart Reading (xAI Grok Vision)"
    mission = "👁️ Look at the chart like a trader - Grok reads the real candle picture before a buy"
    description = ("Draws the coin's candle chart (with volume) and has xAI's Grok vision model read it: bullish, "
                   "bearish or sideways, and the pattern it sees. A confident bearish read trims the buy (or holds it "
                   "back in veto mode); a confident bullish one adds a little conviction. Needs the Grok key; "
                   "a limited number of looks an hour.")
    use_history = False

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.reads: dict[str, dict] = {}
        self.sent: deque[float] = deque(maxlen=500)
        self._tasks: set[asyncio.Task] = set()
        self.last_error = ""

    @property
    def mode(self) -> str:
        return getattr(self.settings, "chart_vision", "nudge")

    def subscriptions(self):
        return {"signal.proposed": self.on_signal}

    def client(self):
        g = self.desk.bots.get("grok")
        return getattr(g, "client", None) if g is not None and g.enabled else None

    def candles(self, symbol: str) -> list[list[float]]:
        scout = self.desk.bots.get("scout")
        book = getattr(scout, "books", {}).get(symbol) if scout is not None else None
        if book is None:
            return []
        f = book.tf.frames.get(getattr(self.settings, "chart_vision_frame", "1h"))
        if f is None:
            return []
        rows = [list(r) for r in f.closed] + ([list(f.cur)] if f.cur is not None else [])
        return rows[-80:]

    def picture(self, symbol: str) -> bytes | None:
        rows = self.candles(symbol)
        return candle_png(rows) if len(rows) >= MIN_CANDLES else None

    def budget_left(self) -> int:
        t = time.time()
        return max(0, int(getattr(self.settings, "chart_vision_per_hour", 30)) - sum(1 for x in self.sent if t - x < 3600))

    def on_signal(self, ev) -> None:
        sig = ev.payload
        if self.mode == "off" or getattr(sig, "side", None) is not Side.BUY or getattr(sig, "kind", None) is None:
            return
        if sig.kind.value != "entry" or (client := self.client()) is None:
            return
        r = self.reads.get(sig.symbol)
        if r and time.time() - r["at"] < FRESH_S:
            return
        if len(self._tasks) >= 2 or self.budget_left() <= 0:
            self.bump("skipped_budget")
            return
        png = self.picture(sig.symbol)
        if png is None:
            self.bump("not_enough_chart")
            return
        self.sent.append(time.time())
        task = asyncio.ensure_future(self._look(client, sig.symbol, png))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _look(self, client, symbol: str, png: bytes) -> None:
        frame = getattr(self.settings, "chart_vision_frame", "1h")
        self.bump("looks")
        try:
            reply = await client.vision(SYSTEM, f"{symbol}, {frame} candles. What is the trend?", png,
                                        getattr(self.settings, "grok_vision_model", ""))
            read = parse_read(reply)
            self.last_error = ""
        except Exception as exc:  # (a failed look is no read: the buy is judged without it)
            self.bump("failures")
            self.last_error = str(exc)[:200]
            return
        self.reads[symbol] = {**read, "frame": frame, "at": time.time()}
        self.bump(read["trend"])
        self.act(f"{symbol} {frame} chart: {read['trend']} ({read['confidence']:.0%}) - {read['pattern'] or read['reason']}")

    def read(self, symbol: str) -> dict | None:
        r = self.reads.get(symbol)
        return r if r and time.time() - r["at"] < FRESH_S else None

    def advise(self, sig, snap):
        if self.mode == "off" or sig.side is not Side.BUY or (r := self.read(sig.symbol)) is None:
            return NEUTRAL
        if r["trend"] == "bearish" and r["confidence"] >= SURE:
            if self.mode == "veto":
                return f"Grok reads the {r['frame']} chart as bearish ({r['confidence']:.0%}): {r['reason']}", 0, 1
            return None, -0.05, 0.8
        if r["trend"] == "bullish" and r["confidence"] >= SURE:
            return None, 0.04, 1.0
        return NEUTRAL

    def vision_status(self) -> dict:
        return {"connected": self.client() is not None, "looks_left_this_hour": self.budget_left(),
                "last_error": self.last_error}
