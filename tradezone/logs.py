"""Keep the server log readable: the same warning repeating (a price source that's down answers every request
with the same error, for every coin, every second) is shown once a minute, with a count of how many were held back.
Important lines (the login code, errors with tracebacks) are never held back."""

from __future__ import annotations

import logging
import time

WINDOW_S = 60.0


class RepeatFilter(logging.Filter):
    def __init__(self, window_s: float = WINDOW_S) -> None:
        super().__init__()
        self.window_s = window_s
        self.seen: dict[tuple[str, str], list[float]] = {}  # (logger, message template) -> [first shown, held back]

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR or record.exc_info or record.levelno < logging.WARNING:
            return True
        if "login code" in str(record.msg) or str(record.msg).startswith("="):
            return True
        key = (record.name, str(record.msg))
        now = time.monotonic()
        entry = self.seen.get(key)
        if entry is not None and now - entry[0] < self.window_s:
            entry[1] += 1
            return False
        held = int(entry[1]) if entry else 0
        self.seen[key] = [now, 0]
        if held:
            record.msg = f"{record.msg} (+{held} more like this in the last minute)"
        if len(self.seen) > 2000:  # (never grows without bound)
            for k in list(self.seen)[:1000]:
                self.seen.pop(k, None)
        return True


def install(window_s: float = WINDOW_S) -> RepeatFilter:
    """Attach the filter to every handler of the root logger (call after logging is configured)."""
    f = RepeatFilter(window_s)
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    return f
