"""Advisors: ten specialists the Risk Manager consults before every NEW trade, plus the Pullback Hunter.

Each advisor looks at one thing and answers with ``advise(signal, snapshot)``:

* a **veto** (a plain reason the trade shouldn't happen now), or
* a **conviction nudge** (a little more or less sure), and
* a **size multiplier** (a bit bigger or smaller).

The Risk Manager combines them: the first veto stops the trade; nudges add up (capped at +/-0.15); size
multipliers multiply (kept between 0.4x and 1.5x). Every hard limit (stops, risk per trade, position and
portfolio caps, loss limits) still applies on top, and exits are never affected. Any advisor can be
switched off from the bot floor.
"""

from __future__ import annotations

import math
import statistics
from collections import deque

from ..core.bus import Event
from ..core.models import Side, Signal, Snapshot, now
from .base import Bot
from .quant import momentum_score

NEUTRAL: tuple[str | None, float, float] = (None, 0.0, 1.0)


class Advisor(Bot):
    advisor = True

    def advise(self, sig: Signal, snap: Snapshot | None) -> tuple[str | None, float, float]:
        return NEUTRAL

    def subscriptions(self):
        return {"market.snapshot": self._snap}

    def _snap(self, ev: Event) -> None:
        if not ev.payload.historical or self.use_history:
            self.on_snapshot(ev.payload)

    use_history = True  # warm-up bars teach most advisors too

    def on_snapshot(self, snap: Snapshot) -> None:
        pass


# ------------------------------------------------------------------------------------------------ 1
class WeatherBot(Advisor):
    name = "weather"
    title = "Market Weather"
    role = "BTC Leadership"
    mission = "🌦️ Don't buy into a storm - when Bitcoin falls hard, the rest of crypto usually follows"
    description = "Watches Bitcoin: while it's dropping fast, no new buys in other coins; when it's strong, a small boost."
    DROP, RISE, BARS = -0.015, 0.01, 15

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.btc: deque[float] = deque(maxlen=self.BARS + 1)
        self.change = 0.0
        self.vol = 0.0  # BTC's usual move per bar

    def on_snapshot(self, snap: Snapshot) -> None:
        if snap.symbol.upper().startswith(("BTC-", "BTC/")):
            self.btc.append(snap.price)
            self.vol = snap.indicators.get("volatility") or 0.0
            if len(self.btc) > 1:
                self.change = snap.price / self.btc[0] - 1
                self.metrics["btc_change"] = round(self.change, 4)

    def drop_limit(self) -> float:
        """A fall counts as a storm at -1.5%, or 3x BTC's normal 15-bar move if that's bigger (a jumpy market)."""
        return -max(-self.DROP, 3 * self.vol * self.BARS ** 0.5)

    def advise(self, sig, snap):
        if not self.settings.is_crypto(sig.symbol) or sig.symbol.upper().startswith("BTC") or len(self.btc) <= self.BARS:
            return NEUTRAL
        if sig.side is Side.BUY and self.change <= self.drop_limit():
            return f"Bitcoin is falling ({self.change:+.1%} in {self.BARS} bars) - other coins usually follow", 0, 1
        if sig.side is Side.BUY and self.change >= self.RISE:
            return None, 0.03, 1.1
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 2
class VolSizerBot(Advisor):
    name = "volsizer"
    title = "Volatility Sizer"
    role = "Volatility-Based Sizing"
    mission = "📏 Steady risk - smaller trades when a coin is unusually wild, bigger when it's calm"
    description = "Compares a coin's volatility now with its usual level and scales the trade size (0.6x - 1.4x)."

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.vols: dict[str, deque[float]] = {}

    def on_snapshot(self, snap):
        v = snap.indicators.get("volatility") or 0.0
        if v > 0:
            self.vols.setdefault(snap.symbol, deque(maxlen=300)).append(v)

    def advise(self, sig, snap):
        h = self.vols.get(sig.symbol)
        if not h or len(h) < 30 or h[-1] <= 0:
            return NEUTRAL
        mult = max(0.6, min(1.4, statistics.median(h) / h[-1]))
        return None, 0.0, mult


# ------------------------------------------------------------------------------------------------ 3
class RankerBot(Advisor):
    name = "ranker"
    title = "Momentum Ranker"
    role = "Relative Strength"
    mission = "🏅 Back the leaders - favour the coins rising most right now, hold back on the laggards"
    description = "Ranks every coin by its 20-bar move: the top third gets a boost, the bottom third is trimmed."

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.closes: dict[str, deque[float]] = {}

    def on_snapshot(self, snap):
        self.closes.setdefault(snap.symbol, deque(maxlen=21)).append(snap.price)

    def strength(self) -> dict[str, float]:
        return {s: c[-1] / c[0] - 1 for s, c in self.closes.items() if len(c) >= 21 and c[0] > 0}

    def advise(self, sig, snap):
        st = self.strength()
        if len(st) < 3 or sig.symbol not in st:
            return NEUTRAL
        order = sorted(st, key=st.get, reverse=sig.side is Side.BUY)  # for a short, the weakest lead
        pos = order.index(sig.symbol) / (len(order) - 1)
        if pos <= 1 / 3:
            return None, 0.05, 1.1
        if pos >= 2 / 3:
            return None, -0.03, 0.85
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 4
class StretchBot(Advisor):
    name = "stretch"
    title = "Overextension Guard"
    role = "No Chasing"
    mission = "🪢 Don't chase - skip coins that already ran far above their average"
    description = "Vetoes buys more than 8 ATR above the slow average or with RSI 90+ (shorts mirrored)."

    def advise(self, sig, snap):
        if snap is None:
            return NEUTRAL
        ind = snap.indicators
        atr = max(ind.get("atr") or 0.0, snap.price * 1e-4)
        stretch = (snap.price - ind.get("ema_slow", snap.price)) / atr * sig.side.sign
        rsi = ind.get("rsi", 50)
        if stretch >= 8 or (sig.side is Side.BUY and rsi >= 90) or (sig.side is Side.SELL and rsi <= 10):
            return f"already stretched {stretch:.1f} ATR from its average (RSI {rsi:.0f}) - not chasing it", 0, 1
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 5
class LevelsBot(Advisor):
    name = "levels"
    title = "Resistance Mapper"
    role = "Support & Resistance"
    mission = "🧱 Mind the ceiling - smaller bets when a recent high sits just above the price"
    description = "Finds the nearest recent high (or low for shorts): if it's within half the target, the trade is trimmed."

    def advise(self, sig, snap):
        if snap is None:
            return NEUTRAL
        ind, px = snap.indicators, snap.price
        target = self.settings.plan_pcts(sig.symbol, self.quick)[1]
        if sig.side is Side.BUY:
            levels = [x for x in (ind.get("swing_high"), ind.get("donchian_high")) if x and x > px]
            room = (min(levels) - px) / px if levels else None
        else:
            levels = [x for x in (ind.get("swing_low"), ind.get("donchian_low")) if x and x < px]
            room = (px - max(levels)) / px if levels else None
        if room is None:
            return None, 0.02, 1.0  # nothing overhead: a clean breakout
        if room < target * 0.5:
            return None, -0.05, 0.8
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 6
class CorrelationBot(Advisor):
    name = "correlation"
    title = "Correlation Officer"
    role = "Diversification"
    mission = "🧩 Spread the bets - don't stack trades that move together"
    description = "Measures how closely coins move together: blocks a buy that moves almost exactly like 2+ coins already held."
    LIMIT = 0.85

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.rets: dict[str, deque[float]] = {}
        self.last: dict[str, float] = {}

    def on_snapshot(self, snap):
        p = self.last.get(snap.symbol)
        if p:
            self.rets.setdefault(snap.symbol, deque(maxlen=60)).append(math.log(snap.price / p))
        self.last[snap.symbol] = snap.price

    def corr(self, a: str, b: str) -> float | None:
        x, y = list(self.rets.get(a, ())), list(self.rets.get(b, ()))
        n = min(len(x), len(y))
        if n < 30:
            return None
        x, y = x[-n:], y[-n:]
        try:
            return statistics.correlation(x, y)
        except statistics.StatisticsError:
            return None

    def advise(self, sig, snap):
        twins = [p.symbol for p in self.portfolio.open_positions
                 if p.symbol != sig.symbol and (p.qty > 0) == (sig.side is Side.BUY)
                 and (c := self.corr(sig.symbol, p.symbol)) is not None and c >= self.LIMIT]
        if len(twins) >= 2:
            return f"moves almost exactly like {', '.join(twins[:3])} already held - no extra diversification", 0, 1
        if twins:
            return None, 0.0, 0.75
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 7
class WhaleBot(Advisor):
    name = "whales"
    title = "Whale Watcher"
    role = "Accumulation Spotting"
    mission = "🐋 Follow the big money - heavy buying while the price holds still often comes before a move"
    description = "Spots bars with 3x the usual volume but almost no price change, and gives buys there a boost for 10 bars."
    use_history = False

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.last: dict[str, float] = {}
        self.marked: dict[str, int] = {}

    def on_snapshot(self, snap):
        p, vr = self.last.get(snap.symbol), snap.indicators.get("volume_ratio")
        self.last[snap.symbol] = snap.price
        if self.marked.get(snap.symbol, 0) > 0:
            self.marked[snap.symbol] -= 1
        if p and vr is not None and vr >= 3 and abs(snap.price / p - 1) < 0.003:
            self.marked[snap.symbol] = 10
            self.bump("spotted")
            self.act(f"{snap.symbol}: {vr:.1f}x volume with the price flat - someone is accumulating")

    def advise(self, sig, snap):
        return (None, 0.05, 1.0) if sig.side is Side.BUY and self.marked.get(sig.symbol, 0) > 0 else NEUTRAL


# ------------------------------------------------------------------------------------------------ 8
class LiquidityBot(Advisor):
    name = "liquidity"
    title = "Liquidity Guard"
    role = "Market Depth"
    mission = "💧 Trade where there are buyers and sellers - not when the market has gone quiet"
    description = "Vetoes buys when trading dries up (under a quarter of the usual volume); trims them when the bid/ask gap is wide."

    def advise(self, sig, snap):
        vr = snap.indicators.get("volume_ratio") if snap is not None else None
        if vr is not None and vr < 0.25:
            return f"trading has dried up ({vr:.0%} of the usual volume)", 0, 1
        q = getattr(self.desk.broker, "quotes", {}).get(sig.symbol) if self.desk.is_live else None
        if q and q.get("ask", 0) > q.get("bid", 0) > 0 and (q["ask"] - q["bid"]) / q["ask"] > 0.008:
            return None, 0.0, 0.7
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 9
class ShockGuardBot(Advisor):
    name = "shockguard"
    title = "Flash-Crash Guard"
    role = "Market-Wide Shocks"
    mission = "🧯 Step back in a panic - pause new buys when several coins crash at once"
    description = "If 3+ coins move 5+ standard deviations in the same bar, or Bitcoin drops 3% in 5 bars, new trades wait 15 minutes."
    use_history = False
    PAUSE_S = 900

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.rets: dict[str, deque[float]] = {}
        self.last: dict[str, float] = {}
        self.btc: deque[float] = deque(maxlen=6)
        self.shocks: dict[int, set[str]] = {}
        self.until = 0.0
        self.clock = 0.0

    def on_snapshot(self, snap):
        self.clock = snap.ts or now()
        p = self.last.get(snap.symbol)
        self.last[snap.symbol] = snap.price
        if snap.symbol.upper().startswith("BTC"):
            self.btc.append(snap.price)
            limit = -max(0.03, 4 * (snap.indicators.get("volatility") or 0.0) * 5 ** 0.5)  # 3%, or 4x a normal 5-bar move
            if len(self.btc) == self.btc.maxlen and snap.price / self.btc[0] - 1 <= limit:
                self._trip(f"Bitcoin {snap.price / self.btc[0] - 1:+.1%} in 5 bars")
        if not p:
            return
        r = math.log(snap.price / p)
        h = self.rets.setdefault(snap.symbol, deque(maxlen=120))
        if len(h) >= 30:
            sd = statistics.pstdev(h)
            if sd > 0 and abs(r) >= 5 * sd:
                scout = self.desk.bots.get("scout")
                bucket = int(self.clock // max(getattr(scout, "bar_seconds", 0) or 1, 1))  # the same bar
                hit = self.shocks.setdefault(bucket, set())
                hit.add(snap.symbol)
                for b in [b for b in self.shocks if b < bucket - 2]:
                    del self.shocks[b]
                if len(hit) >= 3:
                    self._trip(f"{len(hit)} coins moved 5+ standard deviations at once")
        h.append(r)

    def _trip(self, why: str) -> None:
        if self.clock < self.until:
            return
        self.until = self.clock + self.PAUSE_S
        self.bump("trips")
        self.act(f"market shock: {why} - new trades wait 15 min", "warning")

    def advise(self, sig, snap):
        if self.clock < self.until:
            return f"market-wide shock - new trades wait {(self.until - self.clock) / 60:.0f} more min", 0, 1
        return NEUTRAL


# ------------------------------------------------------------------------------------------------ 10
class HTFBot(Advisor):
    name = "htf"
    title = "Big-Picture Trend"
    role = "Higher-Timeframe Trend"
    mission = "🔭 See the bigger picture - trade with the 15-minute trend, not against it"
    description = "Builds 15-bar candles and their 8/21 averages: buys against the bigger trend are trimmed, with it get a nudge."
    STEP = 15

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.count: dict[str, int] = {}
        self.fast: dict[str, float] = {}
        self.slow: dict[str, float] = {}
        self.bars: dict[str, int] = {}

    def on_snapshot(self, snap):
        n = self.count[snap.symbol] = self.count.get(snap.symbol, 0) + 1
        if n % self.STEP:
            return
        x = snap.price
        f, sl = self.fast.get(snap.symbol, x), self.slow.get(snap.symbol, x)
        self.fast[snap.symbol] = f + (x - f) * 2 / 9
        self.slow[snap.symbol] = sl + (x - sl) * 2 / 22
        self.bars[snap.symbol] = self.bars.get(snap.symbol, 0) + 1

    def trend(self, symbol: str) -> int:
        if self.bars.get(symbol, 0) < 21:
            return 0
        f, sl = self.fast[symbol], self.slow[symbol]
        return 1 if f > sl * 1.0005 else -1 if f < sl * 0.9995 else 0

    def advise(self, sig, snap):
        t = self.trend(sig.symbol) * sig.side.sign
        return (None, 0.03, 1.0) if t > 0 else (None, -0.03, 0.8) if t < 0 else NEUTRAL


# ------------------------------------------------------------------------------------------------ 15
class PullbackBot(Bot):
    name = "pullback"
    title = "Pullback Hunter"
    role = "Breakout Retests"
    mission = "🎣 Buy the retest - after a breakout, buy the first pullback to the old high"
    description = ("Marks breakouts above the recent high; if within 8 bars the price dips back to that level and holds "
                   "above it with momentum and normal volume, it proposes a buy.")
    BARS, NEAR = 8, 0.003

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.breaks: dict[str, tuple[float, int]] = {}  # symbol -> (breakout level, bars left)
        self.metrics.update({"breakouts": 0, "retests": 0})

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot}

    def on_snapshot(self, ev: Event) -> None:
        snap: Snapshot = ev.payload
        sym, ind, px = snap.symbol, snap.indicators, snap.price
        if snap.historical or self.desk.halted or self.desk.paused or not self.settings.pullback:
            return
        level = ind.get("donchian_high")
        if level and px > level * 1.002 and sym not in self.breaks:
            self.breaks[sym] = (level, self.BARS)
            self.bump("breakouts")
            return
        if sym not in self.breaks:
            return
        level, left = self.breaks[sym]
        if left <= 0 or px < level * 0.995:  # too late, or the breakout failed
            del self.breaks[sym]
            return
        self.breaks[sym] = (level, left - 1)
        if not (level <= px <= level * (1 + self.NEAR)):
            return
        if not self.portfolio.position(sym).is_flat or self.desk.in_focus(sym):
            return
        vr, mom = ind.get("volume_ratio"), momentum_score(snap)
        if mom <= 0 or (vr is not None and vr < 0.8):
            return
        del self.breaks[sym]
        self.bump("retests")
        sig = Signal(sym, Side.BUY, round(min(0.8, 0.5 + 0.3 * mom), 3),
                     f"pullback: {sym} retested its breakout level {level:,.6g} and held", self.name,
                     votes={"breakout": "yes", "momentum": "yes"})
        self.publish("signal.proposed", sig)
        self.act(f"RETEST {sym} @ {px:,.6g}: back at the breakout level {level:,.6g} and holding")


ADVISORS = [WeatherBot, VolSizerBot, RankerBot, StretchBot, LevelsBot, CorrelationBot, WhaleBot, LiquidityBot,
            ShockGuardBot, HTFBot]
