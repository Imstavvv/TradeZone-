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
        self.last_ts = 0.0
        self.count = 0
        self.bar_bucket: int | None = None  # current bar index (bar mode)
        self.bar_close = 0.0


class ScoutBot(Bot):
    name = "scout"
    title = "Scout"
    role = "Market Data & Signal Prep"
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
            if b.bar_bucket is None:
                b.bar_bucket, b.bar_close = bucket, t.price
                return
            if bucket == b.bar_bucket:
                b.bar_close = t.price
                return
            close, b.bar_bucket, b.bar_close = b.bar_close, bucket, t.price
            self.bump("bars")
            self._on_close(t.symbol, b, close, t.ts)
            return
        self._on_close(t.symbol, b, t.price, t.ts)

    def _on_close(self, symbol: str, b: "_Book", price: float, ts: float) -> None:
        """Update indicators with one closing price (a tick in sim mode, a bar otherwise)."""
        t = Tick(symbol, price, 0.0, ts)
        prev_high = b.donchian.max() if b.donchian.full else None
        prev_low = b.donchian.min() if b.donchian.full else None
        if b.prices:
            r = math.log(t.price / b.prices[-1])
            sigma = b.returns.std()
            if b.returns.full and sigma > 0 and abs(r) > 6 * sigma:
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
        }
        self.publish("market.snapshot", Snapshot(t.symbol, t.price, ind, t.ts))
        self.bump("snapshots")
        self.act(f"{t.symbol} {t.price:,.2f} rsi={ind['rsi']:.0f} vol={ind['volatility']:.2%}")

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
