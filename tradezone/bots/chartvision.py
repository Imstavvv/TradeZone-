"""Chart Vision: xAI's Grok looks at the coin's chart - a real candle picture, the way a trader would.

When a buy is proposed, the coin's last 80 candles (1-hour by default) are drawn as a PNG and sent to Grok's vision
model, which answers with the trend (bullish / bearish / sideways), how sure it is, and the pattern it sees. The
read is kept for 30 minutes; buys use the latest one:

* bearish and fairly sure (70%+): a little less sure and smaller - or, with ``chart_vision = "veto"``, no buy;
* bullish and fairly sure: a little more sure.

Without the Grok key (or between Grok's looks) the built-in chart reader reads the same candles - their structure
(higher highs and lows, or lower), the trend line, and breakouts above or under the last 20 candles - for free.
With the key, Grok's look comes first; it works with the Grok analyst off too. Each look is a paid xAI request, so at most
``chart_vision_per_hour`` an hour, and never more than two at a time. Sells are never touched.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from itertools import pairwise

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


def read_chart(rows: list[list[float]]) -> dict | None:
    """The built-in chart reader (no key, no cost): the last 40 candles' structure, slope and breakouts.

    * structure: the 40 candles in 4 blocks - highs and lows mostly rising (higher highs, higher lows) or falling;
    * slope: the closes' straight-line trend, measured in candle ranges;
    * breakout / breakdown: the close above the last 20 candles' high, or under their low.

    Returns the same shape as Grok's read (trend, confidence, pattern, reason), or None under 30 candles."""
    rows = [r for r in rows if len(r) >= 5 and r[2] >= r[3] > 0][-40:]
    if len(rows) < MIN_CANDLES:
        return None
    hi, lo, cl = [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows]
    n = len(cl)
    rng = sum(h - low for h, low in zip(hi, lo, strict=True)) / n or cl[-1] * 1e-4
    xm, ym = (n - 1) / 2, sum(cl) / n
    slope = sum((i - xm) * (c - ym) for i, c in enumerate(cl)) / sum((i - xm) ** 2 for i in range(n))
    run = slope * n / rng  # (how many candle ranges the trend line climbs over the chart)
    k = n // 4
    bh = [max(hi[i * k:(i + 1) * k]) for i in range(4)]
    bl = [min(lo[i * k:(i + 1) * k]) for i in range(4)]
    hh = sum(b > a for a, b in pairwise(bh))
    hl = sum(b > a for a, b in pairwise(bl))
    structure = 1 if hh >= 2 and hl >= 2 else -1 if hh <= 1 and hl <= 1 else 0
    prior_hi, prior_lo = max(hi[-21:-1]), min(lo[-21:-1])
    brk = 1 if cl[-1] > prior_hi else -1 if cl[-1] < prior_lo else 0
    score = 0.4 * max(-1.0, min(1.0, run / 3)) + 0.35 * structure + 0.25 * brk
    trend = "bullish" if score >= 0.35 else "bearish" if score <= -0.35 else "sideways"
    if trend == "bullish":  # (the pattern named is the one behind the verdict; a signal against it is a caveat)
        pattern = ("breakout above the 20-candle high" if brk > 0 else "higher highs and higher lows" if structure > 0
                   else "uptrend")
        pattern += ", but it just dipped under the 20-candle low" if brk < 0 else ""
    elif trend == "bearish":
        pattern = ("breakdown under the 20-candle low" if brk < 0 else "lower highs and lower lows" if structure < 0
                   else "downtrend")
        pattern += ", but it just poked over the 20-candle high" if brk > 0 else ""
    else:
        pattern = ("range, testing the 20-candle high" if brk > 0 else "range, testing the 20-candle low" if brk < 0
                   else "range")
    return {"trend": trend, "confidence": round(0.5 + 0.5 * min(1.0, abs(score)), 2), "pattern": pattern,
            "reason": f"trend line {run:+.1f} candle ranges over {n} candles; {pattern}", "source": "chart reader"}


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
        return {}  # (a look starts when the Risk Manager asks about a buy that got that far - see advise)

    def client(self):
        """The xAI connection: Chart Vision's own (from the Grok key - it works with the Grok analyst off or
        switched off), else the analyst's."""
        g = self.desk.bots.get("grok")
        if g is None:
            return None
        return getattr(g, "vision_client", None) or getattr(g, "client", None)

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

    def want_look(self, sig) -> None:
        """Start a chart look for a buy the Risk Manager is weighing (not every proposal: the looks are paid and
        limited, so they go to the buys that passed the earlier checks). Its read counts for the next buys."""
        if self.mode == "off" or sig.side is not Side.BUY or getattr(sig.kind, "value", "") != "entry":
            return
        if (client := self.client()) is None:
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
        self.reads[symbol] = {**read, "frame": frame, "at": time.time(), "source": "Grok"}
        self.bump(read["trend"])
        self.act(f"{symbol} {frame} chart: {read['trend']} ({read['confidence']:.0%}) - {read['pattern'] or read['reason']}")

    def grok_read(self, symbol: str) -> dict | None:
        r = self.reads.get(symbol)
        return r if r and time.time() - r["at"] < FRESH_S else None

    def local_read(self, symbol: str) -> dict | None:
        r = read_chart(self.candles(symbol))
        return {**r, "frame": getattr(self.settings, "chart_vision_frame", "1h")} if r else None

    def read(self, symbol: str) -> dict | None:
        """Grok's read of the picture when there's a fresh one, else the built-in chart reader's."""
        return self.grok_read(symbol) or self.local_read(symbol)

    def advise(self, sig, snap):
        if self.mode == "off" or sig.side is not Side.BUY:
            return NEUTRAL
        if self.grok_read(sig.symbol) is None:
            try:
                self.want_look(sig)
            except RuntimeError:  # (no event loop running: no look)
                pass
        r = self.grok_read(sig.symbol)
        if r is None and getattr(self.settings, "chart_vision_local", False):
            r = self.local_read(sig.symbol)  # (the built-in reader changes trades only when switched on)
        if r is None:
            return NEUTRAL
        if r["trend"] == "bearish" and r["confidence"] >= SURE:
            if self.mode == "veto":
                return f"{'the chart reader' if r.get('source') else 'Grok'} reads the {r['frame']} chart as bearish ({r['confidence']:.0%}): {r['reason']}", 0, 1
            return None, -0.05, 0.8
        if r["trend"] == "bullish" and r["confidence"] >= SURE:
            return None, 0.04, 1.0
        return NEUTRAL

    def vision_status(self) -> dict:
        return {"connected": self.client() is not None, "local_trades": bool(getattr(self.settings, "chart_vision_local", False)),
                "looks_left_this_hour": self.budget_left(),
                "last_error": self.last_error}
