"""Strategist - classifies the market regime for every symbol."""

from __future__ import annotations

from collections import deque

from ..core.bus import Event
from ..core.indicators import RollingWindow, efficiency_ratio
from ..core.models import Regime, RegimeUpdate, Snapshot
from .base import Bot


class StrategistBot(Bot):
    name = "strategist"
    title = "Strategist"
    role = "Market Regime Analysis"
    description = (
        "Decides whether each market is trending, ranging, or in a volatility storm, so the "
        "Quant knows which playbook to trust and the Risk Manager can scale exposure."
    )

    WINDOW = 40
    CONFIRM = 5

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.prices: dict[str, deque[float]] = {}
        self.vol_baseline: dict[str, RollingWindow] = {}
        self.regimes: dict[str, RegimeUpdate] = {}
        self._candidate: dict[str, tuple[Regime, int]] = {}

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot}

    def regime(self, symbol: str) -> Regime:
        r = self.regimes.get(symbol)
        return r.regime if r else Regime.UNKNOWN

    def on_snapshot(self, ev: Event) -> None:
        s: Snapshot = ev.payload
        hist = self.prices.setdefault(s.symbol, deque(maxlen=self.WINDOW))
        hist.append(s.price)
        base = self.vol_baseline.setdefault(s.symbol, RollingWindow(300))
        vol = s.indicators["volatility"]
        base.push(vol)
        if len(hist) < 20:
            return
        er = efficiency_ratio(list(hist))
        vol_ratio = vol / base.mean() if base.mean() > 0 else 1.0
        if vol_ratio > 1.6:
            regime, conf = Regime.VOLATILE, min(1.0, (vol_ratio - 1.6) / 1.0 + 0.5)
        elif er > 0.30:
            regime, conf = Regime.TRENDING, min(1.0, er / 0.6)
        else:
            regime, conf = Regime.RANGING, min(1.0, (0.30 - er) / 0.30 + 0.3)
        prev = self.regimes.get(s.symbol)
        if prev is not None and prev.regime != regime:
            # require the new regime to persist before switching (hysteresis)
            cand, n = self._candidate.get(s.symbol, (regime, 0))
            n = n + 1 if cand == regime else 1
            self._candidate[s.symbol] = (regime, n)
            if n < self.CONFIRM:
                return
        self._candidate.pop(s.symbol, None)
        upd = RegimeUpdate(s.symbol, regime, round(conf, 3), {"efficiency": er, "vol_ratio": vol_ratio})
        self.regimes[s.symbol] = upd
        if prev is None or prev.regime != regime:
            self.publish("regime.update", upd)
            self.bump("regime_changes")
            self.act(f"{s.symbol} regime -> {regime.value} (ER={er:.2f}, vol x{vol_ratio:.1f})")
        counts: dict[str, int] = {}
        for r in self.regimes.values():
            counts[r.regime.value] = counts.get(r.regime.value, 0) + 1
        self.metrics["regimes"] = counts
