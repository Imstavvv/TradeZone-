"""Smart Money: who is really buying - the big trades on the tape, and the big-volume bars on the chart.

Two reads, both kept per coin:

* **The tape** (live Kraken prices): the exchange's own recent trades for a coin about to be bought. The biggest
  trades (the top tenth by size, and at least $2,000) are the whales; whale dollars bought minus whale dollars sold,
  over the last 30 minutes, says which way the big money leans.
* **The chart** (every feed, replays too): bars with 2.5x the usual volume, counted up when the price rose on them
  and down when it fell, fading over the last 30 bars. Heavy volume on the way up is buying; on the way down, selling.

It only weighs in on buys. A clear lean the trade's way: a little more sure, a little bigger. Big money selling into
it: a little less sure and smaller - or, with ``smart_money = "veto"``, no buy until the selling stops. Sells and
exits are never touched.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from ..core.models import Side
from .advisors import NEUTRAL, Advisor

TAPE_FOR_S = 120.0  # a coin's tape is read again after this long
TAPE_WINDOW_S = 1800.0  # whale trades in the last 30 minutes count
WHALE_MIN_USD = 2_000.0
LEAN = 0.6  # a lean: 60%+ of the whale dollars on one side
LEAN_MIN_USD = 10_000.0  # ... and at least this much whale money in all
BARS, BIG_BAR = 30, 2.5


def whale_flow(trades: list[dict], since: float) -> dict:
    """Whale buying vs selling in a batch of trades: the top 10% by dollar size (at least $2,000)."""
    recent = [t for t in trades if t["ts"] >= since and t["price"] > 0 and t["qty"] > 0]
    if not recent:
        return {"trades": 0, "whales": 0, "buy_usd": 0.0, "sell_usd": 0.0, "net_usd": 0.0, "lean": 0.0}
    sizes = sorted(t["price"] * t["qty"] for t in recent)
    cut = max(WHALE_MIN_USD, sizes[int(len(sizes) * 0.9)] if len(sizes) >= 10 else WHALE_MIN_USD)
    whales = [t for t in recent if t["price"] * t["qty"] >= cut]
    buy = sum(t["price"] * t["qty"] for t in whales if t["side"] == "buy")
    sell = sum(t["price"] * t["qty"] for t in whales if t["side"] == "sell")
    total = buy + sell
    return {"trades": len(recent), "whales": len(whales), "cut_usd": round(cut, 2), "buy_usd": round(buy, 2),
            "sell_usd": round(sell, 2), "net_usd": round(buy - sell, 2),
            "lean": round((buy - sell) / total, 3) if total > 0 else 0.0}


class SmartMoneyBot(Advisor):
    name = "smartmoney"
    title = "Smart Money"
    role = "Whale Buys & Big Money Flow"
    mission = "🐋 Buy with the big money, not against it - read the whales on the tape and the heavy-volume bars"
    description = ("Reads the exchange's own recent trades for each coin about to be bought: the biggest trades are the "
                   "whales, and which side they took says where the big money leans. Also counts heavy-volume bars up "
                   "vs down on every chart. Buys with the whales get a little boost; buys into whale selling are "
                   "trimmed (or held back, in veto mode). Never touches sells.")
    use_history = False

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.last: dict[str, float] = {}
        self.bars: dict[str, deque[float]] = {}
        self.tape: dict[str, dict] = {}  # symbol -> whale_flow(...) + "at"
        self.wanted: deque[str] = deque(maxlen=50)  # coins with a buy signal: their tape is read next
        self._task: asyncio.Task | None = None

    @property
    def mode(self) -> str:
        return getattr(self.settings, "smart_money", "nudge")

    # -- the chart: heavy-volume bars, up vs down ----------------------------------------------------------
    def on_snapshot(self, snap) -> None:
        p, vr = self.last.get(snap.symbol), snap.indicators.get("volume_ratio")
        self.last[snap.symbol] = snap.price
        q = self.bars.get(snap.symbol)
        if q is None:
            q = self.bars[snap.symbol] = deque(maxlen=BARS)
        if not p or vr is None:
            q.append(0.0)
            return
        ret = snap.price / p - 1
        q.append(min(vr, 6.0) * (1 if ret > 0 else -1 if ret < 0 else 0) if vr >= BIG_BAR else 0.0)

    def bar_flow(self, symbol: str) -> float:
        """Heavy-volume bars, up minus down, the newest counting most (about -10..+10)."""
        q = self.bars.get(symbol)
        if not q:
            return 0.0
        n = len(q)
        return sum(x * (0.5 + 0.5 * (i + 1) / n) for i, x in enumerate(q))

    # -- the tape: the exchange's own trades --------------------------------------------------------------
    def want_tape(self, symbol: str) -> None:
        """Read this coin's tape next: a buy the Risk Manager is weighing (not every proposal - hundreds of coins
        propose, and only a few tapes can be read a cycle)."""
        if self.tape_ok() and symbol not in self.wanted and time.time() - self.tape.get(symbol, {}).get("at", 0.0) >= TAPE_FOR_S:
            self.wanted.append(symbol)

    def tape_ok(self) -> bool:
        return (self.mode != "off" and getattr(self.settings, "smart_money_tape", True)
                and hasattr(self.desk.feed, "trades") and getattr(self.desk.feed, "name", "") == "kraken")

    async def on_cycle(self) -> None:
        if not self.tape_ok() or not self.wanted or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.ensure_future(self._read_tape())  # (in the background: never slows the cycle)

    async def _read_tape(self) -> None:
        t = time.time()
        for _ in range(min(3, len(self.wanted))):  # a few coins a cycle (Kraken's public limit)
            sym = self.wanted.popleft()
            if t - self.tape.get(sym, {}).get("at", 0.0) < TAPE_FOR_S:
                continue
            trades = await self.desk.feed.trades(sym)
            if trades is None:
                self.bump("tape_errors")
                continue
            flow = whale_flow(trades, t - TAPE_WINDOW_S)
            self.tape[sym] = {**flow, "at": t}
            self.bump("tapes_read")
            if flow["whales"]:
                side = "buying" if flow["lean"] >= LEAN else "selling" if flow["lean"] <= -LEAN else "mixed"
                self.act(f"{sym}: {flow['whales']} whale trades in 30 min - {side} "
                         f"(${flow['buy_usd']:,.0f} bought, ${flow['sell_usd']:,.0f} sold)")
        if len(self.tape) > 300:
            for k in sorted(self.tape, key=lambda k: self.tape[k]["at"])[:100]:
                self.tape.pop(k, None)

    def tape_lean(self, symbol: str) -> float | None:
        """+1 = all the whale money bought, -1 = all sold; None without a fresh, big enough read."""
        f = self.tape.get(symbol)
        if not f or time.time() - f["at"] > TAPE_WINDOW_S / 2 or f["buy_usd"] + f["sell_usd"] < LEAN_MIN_USD:
            return None
        return f["lean"]

    def read(self, symbol: str) -> dict:
        """The plain read for the UI and the Debate bot."""
        lean, bars = self.tape_lean(symbol), self.bar_flow(symbol)
        return {"tape_lean": lean, "bar_flow": round(bars, 2), **({"tape": self.tape[symbol]} if symbol in self.tape else {})}

    def advise(self, sig, snap):
        if self.mode == "off" or sig.side is not Side.BUY:
            return NEUTRAL
        self.want_tape(sig.symbol)
        lean, bars = self.tape_lean(sig.symbol), self.bar_flow(sig.symbol)
        if lean is not None and lean <= -LEAN:
            self.bump("whales_selling")
            if self.mode == "veto":
                f = self.tape[sig.symbol]
                return (f"the whales are selling it (${f['sell_usd']:,.0f} sold vs ${f['buy_usd']:,.0f} bought in big "
                        "trades, last 30 min)", 0, 1)
            return None, -0.05, 0.7
        if lean is not None and lean >= LEAN:
            self.bump("whales_buying")
            return None, 0.05, 1.1
        if bars <= -6:
            self.bump("heavy_selling_bars")
            return None, -0.03, 0.85
        if bars >= 6:
            self.bump("heavy_buying_bars")
            return None, 0.03, 1.0
        return NEUTRAL

    def view(self) -> dict:
        t = time.time()
        fresh = {s: f for s, f in self.tape.items() if t - f["at"] < TAPE_WINDOW_S}
        return {"tapes": len(fresh), "buying": sorted(s for s, f in fresh.items() if f["lean"] >= LEAN),
                "selling": sorted(s for s, f in fresh.items() if f["lean"] <= -LEAN)}
