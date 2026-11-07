"""More of the widely used indicators, computed per coin bar by bar from (high, low, close, volume).

Standard settings: ADX / +DI / -DI (14), Aroon (25), Parabolic SAR (0.02 / 0.02 / 0.2), Supertrend (10, 3),
Ichimoku Tenkan / Kijun (9 / 26), TRIX (15), Chaikin Money Flow (20), CCI (20), Williams %R (14), Money Flow
Index (14) and Keltner Channels (20 EMA, 2 x ATR 10). Volume-based ones (CMF, MFI) are None without volume.
"""

from __future__ import annotations

from collections import deque

from .indicators import EMA


class ExtraIndicators:
    def __init__(self) -> None:
        self.bars: deque[tuple[float, float, float, float]] = deque(maxlen=60)  # (high, low, close, volume)
        self.e1, self.e2, self.e3 = EMA(15), EMA(15), EMA(15)  # TRIX
        self.prev_e3: float | None = None
        self.trix: float | None = None
        self.kelt = EMA(20)
        # Parabolic SAR
        self.sar: float | None = None
        self.sar_up = True
        self.ep = 0.0
        self.af = 0.02
        # Supertrend
        self.st_upper: float | None = None
        self.st_lower: float | None = None
        self.st_up = True

    # --------------------------------------------------------------------------------------------- update
    def update(self, high: float, low: float, close: float, volume: float) -> dict:
        high, low = max(high, close), min(low, close)
        prev = self.bars[-1] if self.bars else None
        self.bars.append((high, low, close, volume))
        # TRIX: the rate of change of a triple-smoothed EMA
        v1 = self.e1.update(close)
        v2 = self.e2.update(v1)
        v3 = self.e3.update(v2)
        if self.prev_e3:
            self.trix = v3 / self.prev_e3 - 1
        self.prev_e3 = v3
        kel = self.kelt.update(close)
        self._psar(high, low, prev)
        out: dict = {}
        n = len(self.bars)
        if n < 30:
            return out
        h = [b[0] for b in self.bars]
        lo = [b[1] for b in self.bars]
        c = [b[2] for b in self.bars]
        v = [b[3] for b in self.bars]
        tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, n)]
        atr10 = sum(tr[-10:]) / 10
        out.update(self._adx(h, lo, tr))
        out.update(self._aroon(h, lo))
        # Ichimoku: the midpoints of the last 9 and 26 bars' range
        out["tenkan"] = (max(h[-9:]) + min(lo[-9:])) / 2
        out["kijun"] = (max(h[-26:]) + min(lo[-26:])) / 2
        out["trix"] = self.trix
        out["keltner_upper"], out["keltner_lower"] = kel + 2 * atr10, kel - 2 * atr10
        out["atr10"] = atr10
        # CCI (20): the typical price against its average, in units of its average deviation
        tp = [(h[i] + lo[i] + c[i]) / 3 for i in range(n)]
        t20 = tp[-20:]
        m = sum(t20) / 20
        md = sum(abs(x - m) for x in t20) / 20
        out["cci"] = (tp[-1] - m) / (0.015 * md) if md > 0 else 0.0
        # Williams %R (14): where the close sits in the last 14 bars' range, -100 (bottom) to 0 (top)
        hh, ll = max(h[-14:]), min(lo[-14:])
        out["williams_r"] = -100 * (hh - c[-1]) / (hh - ll) if hh > ll else -50.0
        # Money Flow Index (14) and Chaikin Money Flow (20): volume-weighted (None without volume)
        if sum(v[-20:]) > 0:
            pos = sum(tp[i] * v[i] for i in range(n - 14, n) if tp[i] > tp[i - 1])
            neg = sum(tp[i] * v[i] for i in range(n - 14, n) if tp[i] < tp[i - 1])
            out["mfi"] = 100 * pos / (pos + neg) if pos + neg > 0 else 50.0
            mfv = sum(((c[i] - lo[i]) - (h[i] - c[i])) / (h[i] - lo[i]) * v[i] for i in range(n - 20, n) if h[i] > lo[i])
            out["cmf"] = mfv / sum(v[-20:])
        else:
            out["mfi"] = out["cmf"] = None
        out["psar"], out["psar_up"] = self.sar, self.sar_up
        out.update(self._supertrend(h[-1], lo[-1], c[-1], atr10))
        return out

    # ------------------------------------------------------------------------------------------- the parts
    @staticmethod
    def _adx(h, lo, tr) -> dict:
        n = len(h)
        pdm = [max(h[i] - h[i - 1], 0.0) if h[i] - h[i - 1] > lo[i - 1] - lo[i] else 0.0 for i in range(1, n)]
        mdm = [max(lo[i - 1] - lo[i], 0.0) if lo[i - 1] - lo[i] > h[i] - h[i - 1] else 0.0 for i in range(1, n)]

        def di(j):  # the 14 bars ending at j
            t = sum(tr[j - 13:j + 1])
            return (100 * sum(pdm[j - 13:j + 1]) / t, 100 * sum(mdm[j - 13:j + 1]) / t) if t > 0 else (0.0, 0.0)
        dx = []
        for j in range(13, len(tr)):
            p, m = di(j)
            dx.append(100 * abs(p - m) / (p + m) if p + m > 0 else 0.0)
        p, m = di(len(tr) - 1)
        return {"adx": sum(dx[-14:]) / len(dx[-14:]) if dx else 0.0, "pdi": p, "mdi": m}

    @staticmethod
    def _aroon(h, lo, k: int = 25) -> dict:
        hh, ll = h[-(k + 1):], lo[-(k + 1):]
        i_hi = max(range(len(hh)), key=lambda i: (hh[i], i))  # (the latest of equal highs)
        i_lo = min(range(len(ll)), key=lambda i: (ll[i], -i))
        return {"aroon_up": 100 * i_hi / k, "aroon_down": 100 * i_lo / k}

    def _psar(self, high: float, low: float, prev) -> None:
        if prev is None:
            self.sar, self.ep, self.sar_up = low, high, True
            return
        sar = self.sar + self.af * (self.ep - self.sar)
        before = self.bars[-3] if len(self.bars) >= 3 else prev  # (the two bars before this one)
        if self.sar_up:
            sar = min(sar, prev[1], before[1])  # never above the last two lows (not this bar's: that's the test)
            if low < sar:  # flipped down
                self.sar_up, sar, self.ep, self.af = False, self.ep, low, 0.02
            elif high > self.ep:
                self.ep, self.af = high, min(0.2, self.af + 0.02)
        else:
            sar = max(sar, prev[0], before[0])
            if high > sar:  # flipped up
                self.sar_up, sar, self.ep, self.af = True, self.ep, high, 0.02
            elif low < self.ep:
                self.ep, self.af = low, min(0.2, self.af + 0.02)
        self.sar = sar

    def _supertrend(self, high: float, low: float, close: float, atr: float, mult: float = 3.0) -> dict:
        mid = (high + low) / 2
        up, dn = mid + mult * atr, mid - mult * atr
        prev_close = self.bars[-2][2] if len(self.bars) >= 2 else close
        if self.st_upper is not None and not (up < self.st_upper or prev_close > self.st_upper):
            up = self.st_upper
        if self.st_lower is not None and not (dn > self.st_lower or prev_close < self.st_lower):
            dn = self.st_lower
        if self.st_up and self.st_lower is not None and close < self.st_lower:
            self.st_up = False
        elif not self.st_up and self.st_upper is not None and close > self.st_upper:
            self.st_up = True
        self.st_upper, self.st_lower = up, dn
        return {"supertrend_up": self.st_up, "supertrend": dn if self.st_up else up}
