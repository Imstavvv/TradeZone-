"""Market Scout - ingests raw ticks and turns them into indicator snapshots."""

from __future__ import annotations

import math
from collections import deque

from ..core.bus import Event
from ..core.indicators import EMA, RSI, RollingWindow
from ..core.models import Alert, Snapshot, Tick, now
from .base import Bot

WARMUP = 30


class _Book:
    def __init__(self) -> None:
        self.prices: deque[float] = deque(maxlen=300)
        self.ema_fast = EMA(12)
        self.ema_slow = EMA(26)
        self.sma = RollingWindow(50)
        self.rsi = RSI(14)
        self.returns = RollingWindow(30)
        self.abs_moves = RollingWindow(14)
        self.donchian = RollingWindow(20)
        self.lows = RollingWindow(10)  # swing lookback (bar lows)
        self.highs = RollingWindow(10)
        self.last_ts = 0.0
        self.count = 0
        self.bar_bucket: int | None = None  # current bar index (bar mode)
        self.bar_close = 0.0
        self.bar_low = math.inf
        self.bar_high = -math.inf
        self.bar_volume = 0.0
        self.volumes = RollingWindow(20)  # traded volume of recent bars (for "is this move on heavy volume?")
        # the wider strategy set: MACD, Bollinger Bands, Stochastic, VWAP, OBV, rate of change
        self.macd_signal = EMA(9)
        self.bb = RollingWindow(20)
        self.stoch_lows = RollingWindow(14)
        self.stoch_highs = RollingWindow(14)
        self.pv: deque[tuple[float, float]] = deque(maxlen=20)  # (price x volume, volume) for VWAP
        self.obv = 0.0
        self.obv_hist: deque[float] = deque(maxlen=11)


class ScoutBot(Bot):
    name = "scout"
    title = "Scout"
    role = "Market Data & Signal Prep"
    mission = "🎯 Take trades - never miss a move: watch every coin, every tick"
    description = (
        "Watches every market tick, validates data quality, and computes the indicator "
        "set (EMA, RSI, volatility, ATR, Donchian channels) the rest of the team relies on."
    )
    critical = True

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.books: dict[str, _Book] = {}
        self.stale_after = max(15.0, desk.settings.cycle_seconds * 15)
        self._stale_alerted: set[str] = set()
        # real feeds: build indicators on time bars; the synthetic feed is already bar-like
        self.bar_seconds = desk.settings.bar_seconds if desk.settings.feed != "sim" else 0.0
        self.metrics["bar_seconds"] = self.bar_seconds or "tick"
        self._historical = False

    def subscriptions(self):
        return {"market.tick": self.on_tick}

    def on_tick(self, ev: Event) -> None:
        t: Tick = ev.payload
        if not (t.price > 0 and math.isfinite(t.price)):
            self.bump("bad_ticks")
            self.publish("alert", Alert("warning", f"discarded bad tick {t.symbol}={t.price}", self.name))
            return
        b = self.books.setdefault(t.symbol, _Book())
        self.desk.prices[t.symbol] = t.price
        b.last_ts = now()
        self._stale_alerted.discard(t.symbol)
        self.bump("ticks")
        if self.bar_seconds > 0:
            bucket = int(t.ts // self.bar_seconds)
            if b.bar_bucket is not None and bucket != b.bar_bucket:
                self.bump("bars")
                self._on_close(t.symbol, b, b.bar_close, t.ts, b.bar_low, b.bar_high, b.bar_volume)
                b.bar_bucket = None
            if b.bar_bucket is None:
                b.bar_bucket, b.bar_low, b.bar_high, b.bar_volume = bucket, t.price, t.price, 0.0
            b.bar_volume += max(t.volume, 0.0)
            b.bar_close = t.price
            b.bar_low, b.bar_high = min(b.bar_low, t.price), max(b.bar_high, t.price)
            return
        self._on_close(t.symbol, b, t.price, t.ts, t.price, t.price, max(t.volume, 0.0))

    def _on_close(self, symbol: str, b: _Book, price: float, ts: float,
                  low: float, high: float, volume: float = 0.0) -> None:
        """Update indicators with one bar (a single tick in sim mode)."""
        t = Tick(symbol, price, 0.0, ts)
        prev_high = b.donchian.max() if b.donchian.full else None
        prev_low = b.donchian.min() if b.donchian.full else None
        swing_low = b.lows.min() if b.lows.full else None  # excludes the current bar
        swing_high = b.highs.max() if b.highs.full else None
        if b.prices:
            r = math.log(t.price / b.prices[-1])
            sigma = b.returns.std()
            if b.returns.full and sigma > 0 and abs(r) > 6 * sigma and not self._historical:
                self.publish(
                    "alert",
                    Alert("warning", f"{t.symbol} shock move {r:+.2%} ({abs(r)/sigma:.1f} sigma)", self.name),
                )
                self.bump("shocks")
            b.returns.push(r)
            b.abs_moves.push(abs(t.price - b.prices[-1]))
        b.prices.append(t.price)
        b.ema_fast.update(t.price)
        b.ema_slow.update(t.price)
        b.sma.push(t.price)
        b.rsi.update(t.price)
        b.donchian.push(t.price)
        b.lows.push(low)
        b.highs.push(high)
        avg_vol = b.volumes.mean() if b.volumes.full else 0.0  # the 20 bars BEFORE this one
        b.volumes.push(volume)
        prev_px = b.prices[-2] if len(b.prices) >= 2 else t.price
        macd = (b.ema_fast.value or t.price) - (b.ema_slow.value or t.price)
        b.macd_signal.update(macd)
        b.bb.push(t.price)
        b.stoch_lows.push(low)
        b.stoch_highs.push(high)
        if volume > 0:
            b.pv.append((t.price * volume, volume))
            b.obv += volume if t.price > prev_px else -volume if t.price < prev_px else 0.0
        b.obv_hist.append(b.obv)
        b.count += 1

        if b.count < WARMUP:
            unit = f"{self.bar_seconds:g}s bars" if self.bar_seconds else "ticks"
            self.act(f"warming up {t.symbol} ({b.count}/{WARMUP} {unit})")
            return
        ind = {
            "ema_fast": b.ema_fast.value or t.price,
            "ema_slow": b.ema_slow.value or t.price,
            "sma50": b.sma.mean(),
            "rsi": b.rsi.value,
            "volatility": b.returns.std(),
            "atr": b.abs_moves.mean(),
            "donchian_high": prev_high if prev_high is not None else t.price,
            "donchian_low": prev_low if prev_low is not None else t.price,
            "swing_low": swing_low if swing_low is not None else min(low, t.price),
            "swing_high": swing_high if swing_high is not None else max(high, t.price),
            # this bar's traded volume vs the recent average (None when the feed doesn't report volume)
            "volume_ratio": volume / avg_vol if avg_vol > 0 else None,
            **self._more(b, t.price, macd),
        }
        self.publish("market.snapshot", Snapshot(t.symbol, t.price, ind, t.ts, historical=self._historical))
        self.bump("snapshots")
        self.act(f"{t.symbol} {t.price:,.2f} rsi={ind['rsi']:.0f} vol={ind['volatility']:.2%}")

    @staticmethod
    def _more(b: _Book, price: float, macd: float) -> dict:
        """MACD histogram, Bollinger Bands, Stochastic %K, VWAP, OBV slope and 10-bar rate of change."""
        mid, sd = b.bb.mean(), b.bb.std()
        lo, hi = b.stoch_lows.min(), b.stoch_highs.max()
        vol_sum = sum(v for _, v in b.pv)
        avg_vol = vol_sum / len(b.pv) if b.pv else 0.0
        out = {
            "macd_hist": macd - (b.macd_signal.value or macd),
            "bb_mid": mid, "bb_upper": mid + 2 * sd, "bb_lower": mid - 2 * sd,
            "stoch_k": 100 * (price - lo) / (hi - lo) if hi > lo else 50.0,
            "roc10": price / b.prices[-11] - 1 if len(b.prices) > 10 and b.prices[-11] > 0 else 0.0,
            "vwap": sum(pv for pv, _ in b.pv) / vol_sum if vol_sum > 0 and len(b.pv) >= 10 else None,
            # OBV's change over 10 bars, in units of an average bar's volume (None without volume data)
            "obv_slope": (b.obv_hist[-1] - b.obv_hist[0]) / (10 * avg_vol) if avg_vol > 0 and len(b.obv_hist) > 10 else None,
        }
        return out

    def backfill(self, symbol: str, candles: list[dict]) -> int:
        """Feed historical candles through the indicators (no trading on them)."""
        b = self.books.setdefault(symbol, _Book())
        self._historical = True
        try:
            for c in candles:
                self._on_close(symbol, b, c["close"], c["ts"], c["low"], c["high"], float(c.get("volume") or 0.0))
        finally:
            self._historical = False
        if candles:
            self.desk.prices.setdefault(symbol, candles[-1]["close"])
            b.last_ts = now()
            self.bump("backfilled_bars", len(candles))
        return len(candles)

    def warmup_status(self) -> dict | None:
        """Progress toward the first indicator snapshot, or None once every symbol is warm."""
        if not self.books:
            return {"done": 0, "needed": WARMUP, "unit": "bars" if self.bar_seconds else "ticks",
                    "bar_seconds": self.bar_seconds}
        done = min(b.count for b in self.books.values())
        if done >= WARMUP:
            return None
        return {"done": done, "needed": WARMUP, "unit": "bars" if self.bar_seconds else "ticks",
                "bar_seconds": self.bar_seconds}

    def history(self, symbol: str) -> list[float]:
        b = self.books.get(symbol)
        return list(b.prices) if b else []

    async def on_cycle(self) -> None:
        t = now()
        for sym, b in self.books.items():
            if t - b.last_ts > self.stale_after and sym not in self._stale_alerted:
                self._stale_alerted.add(sym)
                self.publish("alert", Alert("warning", f"feed stale for {sym}", self.name))
        self.metrics["symbols"] = len(self.books)
