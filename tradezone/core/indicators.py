"""Streaming technical indicators (O(1) updates per tick)."""

from __future__ import annotations

import math
from collections import deque
from itertools import pairwise


class EMA:
    def __init__(self, period: int) -> None:
        self.k = 2.0 / (period + 1)
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.k * (x - self.value)
        return self.value


class RSI:
    """Wilder's RSI."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.prev: float | None = None
        self.avg_gain = 0.0
        self.avg_loss = 0.0
        self.count = 0
        self.value = 50.0

    def update(self, x: float) -> float:
        if self.prev is None:
            self.prev = x
            return self.value
        change = x - self.prev
        self.prev = x
        gain, loss = max(change, 0.0), max(-change, 0.0)
        self.count += 1
        if self.count <= self.period:
            self.avg_gain += gain / self.period
            self.avg_loss += loss / self.period
        else:
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        if self.avg_loss == 0:
            self.value = 100.0 if self.avg_gain > 0 else 50.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class RollingWindow:
    def __init__(self, size: int) -> None:
        self.data: deque[float] = deque(maxlen=size)

    def push(self, x: float) -> None:
        self.data.append(x)

    @property
    def full(self) -> bool:
        return len(self.data) == self.data.maxlen

    def mean(self) -> float:
        return sum(self.data) / len(self.data) if self.data else 0.0

    def std(self) -> float:
        n = len(self.data)
        if n < 2:
            return 0.0
        m = self.mean()
        return math.sqrt(sum((v - m) ** 2 for v in self.data) / (n - 1))

    def max(self) -> float:
        return max(self.data) if self.data else 0.0

    def min(self) -> float:
        return min(self.data) if self.data else 0.0


def efficiency_ratio(prices: list[float]) -> float:
    """Kaufman efficiency ratio: 1 = perfectly directional, 0 = pure noise."""
    if len(prices) < 2:
        return 0.0
    path = sum(abs(b - a) for a, b in pairwise(prices))
    if path == 0:
        return 0.0
    return abs(prices[-1] - prices[0]) / path
