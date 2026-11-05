"""The server log stays readable when a price source is down."""

import logging

from tradezone.logs import RepeatFilter


def rec(msg, *args, level=logging.WARNING, name="tradezone.market.coinbase"):
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


def test_the_same_warning_is_shown_once_a_minute_with_a_count():
    f = RepeatFilter(window_s=60)
    shown = [f.filter(rec("coinbase ticker %s failed: %s", sym, "403")) for sym in ("BTC", "ETH", "SOL") * 10]
    assert shown.count(True) == 1  # 30 failures, one line
    f.seen[("tradezone.market.coinbase", "coinbase ticker %s failed: %s")][0] -= 61  # a minute later
    r = rec("coinbase ticker %s failed: %s", "BTC", "403")
    assert f.filter(r) and "+29 more like this" in r.getMessage()


def test_errors_info_and_the_login_code_are_never_held_back():
    f = RepeatFilter(window_s=60)
    assert all(f.filter(rec("boom", level=logging.ERROR)) for _ in range(5))
    assert all(f.filter(rec("ok", level=logging.INFO)) for _ in range(5))
    assert all(f.filter(rec("  TradeZone login code: %s", "1234-5678", name="tradezone.security")) for _ in range(3))
    assert f.filter(rec("other warning")) and f.filter(rec("a different warning"))  # different messages both show
