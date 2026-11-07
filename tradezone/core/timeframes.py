"""Every timeframe, for every coin: 5-minute, 15-minute, 1-hour, 4-hour and daily candles built from the same price
stream as the 1-minute bars (and seeded with the exchange's own longer history at start-up), so the team sees the
whole chart - not only the last few hours of minutes.

For each timeframe: the trend (fast vs slow average of the closes), RSI(14), the last candle's change, and where the
price sits in that timeframe's range. Plus how many timeframes agree (``mtf_up`` / ``mtf_down`` / ``mtf_score``).

Kept up incrementally: each closed candle updates its timeframe's numbers once, and the candle still forming is
added on top for the view (hundreds of coins x 5 timeframes on every bar must stay cheap)."""

from __future__ import annotations

from collections import deque

TIMEFRAMES: tuple[tuple[str, int], ...] = (("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14_400), ("1d", 86_400))
KEEP = 300  # candles kept per timeframe (300 days of daily candles: the full chart)
FAST, SLOW, RSI_N = 12, 26, 14
_KF, _KS, _A = 2 / (FAST + 1), 2 / (SLOW + 1), 1 / RSI_N


class _Frame:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self.closed: deque[list[float]] = deque(maxlen=KEEP)  # [bucket, open, high, low, close, volume]
        self.cur: list[float] | None = None
        self._reset()

    def _reset(self) -> None:
        self.ema_f = self.ema_s = None
        self.gain = self.loss = 0.0
        self.lo, self.hi = float("inf"), float("-inf")

    def _close(self, row: list[float]) -> None:
        prev = self.closed[-1][4] if self.closed else None
        self.closed.append(row)
        c = row[4]
        self.ema_f = c if self.ema_f is None else self.ema_f + _KF * (c - self.ema_f)
        self.ema_s = c if self.ema_s is None else self.ema_s + _KS * (c - self.ema_s)
        if prev is not None:
            d = c - prev
            self.gain += _A * (max(d, 0.0) - self.gain)
            self.loss += _A * (max(-d, 0.0) - self.loss)
        if len(self.closed) == KEEP or row[3] < self.lo or row[2] > self.hi:  # (the oldest may have dropped out)
            self.lo = min(r[3] for r in self.closed)
            self.hi = max(r[2] for r in self.closed)

    def update(self, ts: float, high: float, low: float, close: float, volume: float) -> None:
        bucket = int(ts // self.seconds)
        if self.cur is not None and bucket > self.cur[0]:
            self._close(self.cur)
            self.cur = None
        if self.cur is None:
            self.cur = [bucket, close, high, low, close, volume]
        elif bucket == self.cur[0]:
            self.cur[2], self.cur[3] = max(self.cur[2], high), min(self.cur[3], low)
            self.cur[4], self.cur[5] = close, self.cur[5] + volume

    def seed(self, candles: list[dict]) -> None:
        """The exchange's own candles for this timeframe: they come before what was built live."""
        by = {}
        for c in candles:
            b = int(float(c["ts"]) // self.seconds)
            by[b] = [b, float(c.get("open") or c["close"]), float(c["high"]), float(c["low"]), float(c["close"]),
                     float(c.get("volume") or 0.0)]
        rows = [by[k] for k in sorted(by)]
        if not rows:
            return
        live = [r for r in self.closed if r[0] > rows[-1][0]]
        if self.cur is not None and self.cur[0] <= rows[-1][0]:
            self.cur = None  # (that candle is in the exchange's history now)
        self.closed = deque(maxlen=KEEP)
        self._reset()
        for r in [*rows, *live][-KEEP:]:
            self._close(r)

    def view(self) -> dict:
        n = len(self.closed) + (self.cur is not None)
        if n < 3 or self.ema_f is None:
            return {}
        c = self.cur[4] if self.cur is not None else self.closed[-1][4]
        fast, slow, gain, loss = self.ema_f, self.ema_s, self.gain, self.loss
        prev = self.closed[-1][4]
        if self.cur is not None:  # the candle still forming, one step on top
            fast, slow = fast + _KF * (c - fast), slow + _KS * (c - slow)
            d = c - prev
            gain, loss = gain + _A * (max(d, 0.0) - gain), loss + _A * (max(-d, 0.0) - loss)
            lo, hi = min(self.lo, self.cur[3]), max(self.hi, self.cur[2])
        else:
            lo, hi = self.lo, self.hi
            prev = self.closed[-2][4] if len(self.closed) > 1 else c
        trend = 0 if n < SLOW // 2 else (1 if fast > slow * 1.0005 else -1 if fast < slow * 0.9995 else 0)
        rsi = 100.0 if loss <= 0 else 100 - 100 / (1 + gain / loss)
        return {"trend": trend, "rsi": rsi, "chg": c / prev - 1 if prev > 0 else 0.0,
                "range_pos": (c - lo) / (hi - lo) if hi > lo else 0.5, "candles": len(self.closed)}


class Timeframes:
    def __init__(self) -> None:
        self.frames = {label: _Frame(sec) for label, sec in TIMEFRAMES}

    def update(self, ts: float, high: float, low: float, close: float, volume: float = 0.0) -> None:
        for f in self.frames.values():
            f.update(ts, high, low, close, volume)

    def seed(self, label: str, candles: list[dict]) -> None:
        if label in self.frames and candles:
            self.frames[label].seed(candles)

    def view(self) -> dict:
        """Flat numbers for a snapshot: tf_<frame>_trend / _rsi / _chg / _range_pos, plus the agreement."""
        out: dict = {}
        up = down = ready = 0
        for label, f in self.frames.items():
            v = f.view()
            if not v:
                continue
            for k, x in v.items():
                out[f"tf_{label}_{k}"] = x
            ready += 1
            up += v["trend"] > 0
            down += v["trend"] < 0
        if ready:
            out.update(mtf_up=up, mtf_down=down, mtf_ready=ready, mtf_score=(up - down) / ready)
        return out
