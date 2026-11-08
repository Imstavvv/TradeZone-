"""Kill what fails (asked for): the features the bots did better without on unseen data are off, once, for good."""

from tests.helpers import run
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed
from tradezone.persistence import StateStore


def boot(db):
    s = Settings(feed="sim", symbols=["BTC-USD"])
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))


def test_the_failed_features_are_off_by_default():
    s = Settings()
    assert not any(getattr(s, k) for k in ("trailing_stop", "live_quick_flips", "flipper", "patient_exits", "coach_learning",
                                           "surge_snipe", "dip_buy", "learn_sources"))
    assert "macd" not in s.strategies and set(s.bots_off) == {"volsizer", "ranker", "reviewer", "stretch"}
    d = boot(":memory:")
    assert not d.bots["volsizer"].enabled and not d.bots["ranker"].enabled and d.bots["weather"].enabled


def test_saved_choices_from_before_lose_the_failed_ones_once_and_a_later_choice_is_kept(tmp_path):
    db = str(tmp_path / "tz.db")
    d = boot(db)
    store = StateStore(d.journal)
    store.save("finders", {"snipe": True, "surge_snipe": True, "pullback": True, "flipper": True})  # (saved before)
    store.save("strategies", {"on": ["macd", "obv", "mfi"], "mfi_offered": True})
    run(d.start())
    assert not d.settings.surge_snipe and not d.settings.flipper and d.settings.snipe and d.settings.pullback
    assert d.settings.strategies == ["obv", "mfi"]
    d.set_strategy("flipper", True)  # switched back on by you...
    d.set_strategy("macd", True)
    run(d.shutdown())
    d2 = boot(db)
    run(d2.start())
    assert d2.settings.flipper and "macd" in d2.settings.strategies  # ...and kept
    run(d2.shutdown())
