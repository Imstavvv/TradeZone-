"""A candle chart as a PNG picture, drawn with nothing but the standard library (no imaging packages needed):
green and red candles with their wicks, a volume strip underneath, and a faint grid - the picture the Chart
Vision bot shows to xAI's Grok."""

from __future__ import annotations

import struct
import zlib

BG = (16, 20, 28)
GRID = (38, 44, 56)
UP = (38, 194, 129)
DOWN = (235, 77, 75)
VOL_UP = (24, 98, 72)
VOL_DOWN = (120, 44, 46)


def _png(width: int, height: int, rows: list[bytearray]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)  # (filter type 0 on every row)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class _Canvas:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.rows = [bytearray(BG * w) for _ in range(h)]

    def rect(self, x0: int, y0: int, x1: int, y1: int, rgb: tuple[int, int, int]) -> None:
        x0, x1 = max(0, min(x0, x1)), min(self.w - 1, max(x0, x1))
        y0, y1 = max(0, min(y0, y1)), min(self.h - 1, max(y0, y1))
        if x1 < x0 or y1 < y0:
            return
        span = bytes(rgb) * (x1 - x0 + 1)
        for y in range(y0, y1 + 1):
            self.rows[y][x0 * 3:(x1 + 1) * 3] = span


def candle_png(candles: list[list[float]] | list[dict], width: int = 640, height: int = 400) -> bytes:
    """``candles``: oldest first, each [ts, open, high, low, close, volume] or a dict with those names."""
    rows = [c if isinstance(c, (list, tuple)) else
            [c.get("ts", 0), c.get("open", c["close"]), c["high"], c["low"], c["close"], c.get("volume", 0.0)]
            for c in candles]
    rows = [[float(x) for x in r[:6]] for r in rows if len(r) >= 5 and r[2] >= r[3] > 0]
    cv = _Canvas(width, height)
    if not rows:
        return _png(width, height, cv.rows)
    pad, vol_h = 8, height // 5
    top, price_bottom = pad, height - vol_h - 2 * pad
    lo, hi = min(r[3] for r in rows), max(r[2] for r in rows)
    if hi <= lo:
        hi, lo = lo * 1.001 + 1e-12, lo * 0.999
    for i in range(1, 5):  # grid
        y = top + (price_bottom - top) * i // 5
        cv.rect(0, y, width - 1, y, GRID)

    def y_of(p: float) -> int:
        return int(price_bottom - (p - lo) / (hi - lo) * (price_bottom - top))

    n = len(rows)
    step = (width - 2 * pad) / n
    body = max(1, int(step * 0.7))
    vmax = max((r[5] if len(r) > 5 else 0.0) for r in rows) or 1.0
    for i, r in enumerate(rows):
        o, h, low, c = r[1], r[2], r[3], r[4]
        v = r[5] if len(r) > 5 else 0.0
        x = int(pad + i * step + step / 2)
        up = c >= o
        cv.rect(x, y_of(h), x, y_of(low), UP if up else DOWN)  # wick
        cv.rect(x - body // 2, y_of(max(o, c)), x - body // 2 + body - 1, y_of(min(o, c)), UP if up else DOWN)
        vh = int(v / vmax * (vol_h - 2))
        if vh > 0:
            cv.rect(x - body // 2, height - pad - vh, x - body // 2 + body - 1, height - pad, VOL_UP if up else VOL_DOWN)
    return _png(width, height, cv.rows)
