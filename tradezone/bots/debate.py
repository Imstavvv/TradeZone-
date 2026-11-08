"""Debate: before every buy, a bull argues for it and a bear argues against it - and the stronger case wins.

The bull and the bear each make their points from the same evidence: the trend on every timeframe (1h, 4h, daily),
momentum (MACD, RSI), where the price sits (VWAP, the 50-bar average, the top of its range), trend strength (ADX),
volume flow (OBV), and what the Smart Money and Chart Vision bots have seen. Each point carries a weight; the margin
between the two cases is the verdict:

* the bear wins clearly (by 2+): a little less sure and smaller; by 5+ with ``debate = "veto"``: no buy;
* the bull wins clearly: a little more sure;
* too close to call: no change.

By default (``debate = "show"``) every buy is still debated and the case is shown on the Read sheet, but trades
aren't changed: nudging them lost money on unseen replays (-$44.83 over the rising, falling and flat markets).

With ``debate_grok`` on (and the Grok key), Grok also judges each debate as a neutral referee; its ruling counts for
that coin's next buys (30 minutes). The last debates are kept for the bot floor. Sells are never touched.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque

from ..core.models import Side
from .advisors import NEUTRAL, Advisor

CLEAR, STRONG = 2.0, 5.0
JUDGE_FOR_S = 1800.0

JUDGE = """You are the neutral judge of a trading debate. A bull and a bear have each made their case about buying a
coin right now. Weigh the quality of the evidence, not the number of points. Respond ONLY with JSON:
{"winner": "bull" | "bear" | "draw", "confidence": <0..1>, "reason": "<max 25 words>"}"""


def _trend_word(t: float) -> str:
    return "up" if t > 0 else "down"


def make_case(ind: dict, price: float, smart: dict | None = None, vision: dict | None = None) -> dict:
    """The two cases, from one set of evidence: {"bull": [(weight, point)], "bear": [...], "margin": bull - bear}."""
    bull: list[tuple[float, str]] = []
    bear: list[tuple[float, str]] = []

    def side(cond_bull: bool, cond_bear: bool, w: float, bull_pt: str, bear_pt: str) -> None:
        if cond_bull:
            bull.append((w, bull_pt))
        elif cond_bear:
            bear.append((w, bear_pt))

    for tf, w in (("1h", 1.0), ("4h", 1.0), ("1d", 1.5)):
        t = ind.get(f"tf_{tf}_trend")
        if t:
            side(t > 0, t < 0, w, f"the {tf} trend is up", f"the {tf} trend is down")
    if (m := ind.get("macd_hist")) is not None:
        side(m > 0, m < 0, 0.5, "MACD momentum is rising", "MACD momentum is fading")
    if (v := ind.get("vwap")) and price:
        side(price > v, price < v, 0.5, "the price is above VWAP (buyers in control today)", "the price is under VWAP")
    if (s := ind.get("sma50")) and price:
        side(price > s, price < s, 0.5, "above its 50-bar average", "below its 50-bar average")
    adx, pdi, mdi = ind.get("adx"), ind.get("pdi"), ind.get("mdi")
    if adx is not None and pdi is not None and mdi is not None and adx >= 20:
        side(pdi > mdi, mdi > pdi, 1.0, f"a strong trend (ADX {adx:.0f}) led by buyers",
             f"a strong trend (ADX {adx:.0f}) led by sellers")
    if (st := ind.get("supertrend_up")) is not None:
        side(bool(st), not st, 0.5, "Supertrend says up", "Supertrend says down")
    if (o := ind.get("obv_slope")) is not None:
        side(o > 0, o < 0, 0.5, "volume is flowing in (OBV rising)", "volume is flowing out (OBV falling)")
    if (r := ind.get("rsi")) is not None:
        if r >= 75:
            bear.append((1.0, f"overbought (RSI {r:.0f}) - late to the move"))
        elif r <= 30:
            bull.append((0.5, f"oversold (RSI {r:.0f}) - room to bounce"))
        elif 45 <= r <= 65:
            bull.append((0.5, f"healthy momentum (RSI {r:.0f})"))
    if (rp := ind.get("tf_1d_range_pos")) is not None and rp >= 0.95:
        bear.append((0.5, "at the very top of its daily range - chasing"))
    if (h := ind.get("tf_1h_rsi")) is not None and h >= 80:
        bear.append((0.5, f"the hourly chart is stretched (RSI {h:.0f})"))
    if smart:
        lean, bars = smart.get("tape_lean"), smart.get("bar_flow") or 0.0
        if lean is not None:
            side(lean >= 0.6, lean <= -0.6, 1.5, "the whales are buying it", "the whales are selling it")
        side(bars >= 6, bars <= -6, 0.5, "heavy-volume bars are up bars", "heavy-volume bars are down bars")
    if vision and vision.get("confidence", 0) >= 0.7:
        who = "the chart reader" if vision.get("source") == "chart reader" else "Grok"
        side(vision["trend"] == "bullish", vision["trend"] == "bearish", 1.5,
             f"{who} reads the chart as bullish ({vision.get('pattern') or 'its picture'})",
             f"{who} reads the chart as bearish ({vision.get('pattern') or 'its picture'})")
    b, s = sum(w for w, _ in bull), sum(w for w, _ in bear)
    return {"bull": bull, "bear": bear, "bull_score": round(b, 2), "bear_score": round(s, 2), "margin": round(b - s, 2)}


class DebateBot(Advisor):
    name = "debate"
    title = "Bull vs Bear Debate"
    role = "Bullish / Bearish Debate"
    mission = "⚖️ Argue both sides before every buy - only the stronger case gets the money"
    description = ("Before each buy, a bull makes the case for it and a bear the case against it, from the trend on "
                   "every timeframe, momentum, VWAP, trend strength, volume flow, the whales and Grok's chart read. "
                   "The bear winning clearly trims the buy (or holds it back in veto mode); the bull winning adds a "
                   "little conviction. Grok can judge the debates too.")
    use_history = False

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.debates: deque[dict] = deque(maxlen=30)
        self.rulings: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def mode(self) -> str:
        return getattr(self.settings, "debate", "nudge")

    def evidence(self, symbol: str) -> tuple[dict | None, dict | None]:
        sm, cv = self.desk.bots.get("smartmoney"), self.desk.bots.get("chartvision")
        smart = sm.read(symbol) if sm is not None and sm.enabled else None
        vision = cv.read(symbol) if cv is not None and cv.enabled else None
        return smart, vision

    def debate(self, symbol: str, snap) -> dict:
        smart, vision = self.evidence(symbol)
        case = make_case(snap.indicators, snap.price, smart, vision)
        m = case["margin"]
        case["verdict"] = "bull" if m >= CLEAR else "bear" if m <= -CLEAR else "too close"
        return case

    def advise(self, sig, snap):
        if self.mode == "off" or sig.side is not Side.BUY or snap is None:
            return NEUTRAL
        case = self.debate(sig.symbol, snap)
        m = case["margin"]
        ruling = self.rulings.get(sig.symbol)
        if ruling and time.time() - ruling["at"] < JUDGE_FOR_S and ruling["confidence"] >= 0.6:
            m += {"bull": 1.5, "bear": -1.5}.get(ruling["winner"], 0.0)  # (the referee's ruling counts too)
        self.debates.append({"symbol": sig.symbol, "ts": time.time(), "verdict": case["verdict"], "margin": m,
                             "bull": [p for _, p in case["bull"]][:6], "bear": [p for _, p in case["bear"]][:6]})
        self.bump(f"won_{case['verdict'].replace(' ', '_')}")
        self.metrics["last"] = f"{sig.symbol}: {case['verdict']} ({case['bull_score']:g} vs {case['bear_score']:g})"
        if getattr(self.settings, "debate_grok", False):
            self._ask_judge(sig.symbol, case)
        if self.mode == "show":
            return NEUTRAL  # (debated and shown, trades unchanged: nudging lost money on unseen replays)
        if m <= -STRONG:
            if self.mode == "veto":
                return (f"the bear wins the debate {case['bear_score']:g} to {case['bull_score']:g}: "
                        + "; ".join(p for _, p in case["bear"][:3]), 0, 1)
            return None, -0.06, 0.75
        if m <= -CLEAR:
            return None, -0.04, 0.85
        if m >= STRONG:
            return None, 0.05, 1.0
        if m >= CLEAR:
            return None, 0.03, 1.0
        return NEUTRAL

    def _ask_judge(self, symbol: str, case: dict) -> None:
        g = self.desk.bots.get("grok")
        client = getattr(g, "client", None) if g is not None and g.enabled else None
        r = self.rulings.get(symbol)
        if client is None or len(self._tasks) >= 2 or (r and time.time() - r["at"] < JUDGE_FOR_S):
            return
        self.rulings[symbol] = {"winner": "draw", "confidence": 0.0, "reason": "asked", "at": time.time()}
        task = asyncio.ensure_future(self._judge(client, symbol, case))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _judge(self, client, symbol: str, case: dict) -> None:
        text = json.dumps({"coin": symbol, "bull_case": [p for _, p in case["bull"]],
                           "bear_case": [p for _, p in case["bear"]]})
        try:
            reply = await client.complete(JUDGE, text)
            m = re.search(r"\{.*\}", reply, re.DOTALL)
            d = json.loads(m.group(0)) if m else {}
            winner = str(d.get("winner", "draw")).lower()
            ruling = {"winner": winner if winner in ("bull", "bear", "draw") else "draw",
                      "confidence": max(0.0, min(1.0, float(d.get("confidence", 0.0)))),
                      "reason": str(d.get("reason", ""))[:200], "at": time.time()}
        except Exception:  # (no ruling: the debate stands on its own points)
            self.bump("judge_failures")
            return
        self.rulings[symbol] = ruling
        self.act(f"Grok judges the {symbol} debate: {ruling['winner']} ({ruling['confidence']:.0%}) - {ruling['reason']}")

    def recent(self) -> list[dict]:
        return list(self.debates)[::-1]
