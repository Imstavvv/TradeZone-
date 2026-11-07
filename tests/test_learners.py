"""Learning bots: the Target Learner (each coin's take-profit) and the Trade Reviewer (each trade finder's record)."""

import pytest

from tests.helpers import run
from tradezone.bots.learners import source_of
from tradezone.config import Settings
from tradezone.core.models import Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed


def make(db=":memory:"):
    s = Settings(feed="sim", symbols=["BTC-USD", "SOL-USD", "DOGE-USD"])
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(db))


def trade(desk, sym, entry, best, exit_, reason="momentum lead in trending regime"):
    """One finished trade: opened at ``entry``, ran up to ``best``, closed at ``exit_``."""
    pub = desk.bus.publish
    pub("trade.log", {"symbol": sym, "side": "buy", "action": "open", "price": entry, "position_after": 1.0,
                      "reason": reason, "pnl": None})
    pub("market.tick", Tick(sym, best))
    pub("trade.log", {"symbol": sym, "side": "sell", "action": "close", "price": exit_, "position_after": 0.0,
                      "reason": "x", "pnl": exit_ - entry})
    run(desk.bus.drain())


@pytest.mark.old_plan  # (a mechanism test, written around the stops / targets before -7% / +15%)
def test_target_learner_lets_winners_run_further_in_coins_whose_moves_run_further():
    desk = make()
    tl = desk.bots["targets"]
    usual = desk.settings.plan_pcts("SOL-USD", True)[1]
    assert tl.target("SOL-USD", usual) == usual  # nothing learned yet
    for _ in range(10):
        trade(desk, "SOL-USD", 100.0, 106.0, 105.5)  # SOL's winners run about +6%
    assert tl.target("SOL-USD", usual) == pytest.approx(0.06, abs=0.001)  # aims there instead of +4%
    for _ in range(10):
        trade(desk, "DOGE-USD", 1.0, 1.5, 1.4)  # a huge run: capped at 1.6x the usual target
    meme = desk.settings.plan_pcts("DOGE-USD", True)[1]
    assert tl.target("DOGE-USD", meme) == pytest.approx(1.6 * meme)


def test_it_never_lowers_a_target_and_learns_only_from_winners():
    desk = make()
    tl = desk.bots["targets"]
    for _ in range(10):
        trade(desk, "SOL-USD", 100.0, 103.0, 102.0)  # winners that only ran +3%
    assert tl.target("SOL-USD", 0.04) == 0.04  # lowering targets sold winners too soon in testing
    for _ in range(10):
        trade(desk, "BTC-USD", 100.0, 108.0, 97.0)  # losers that had run up first
    assert "BTC-USD" not in tl.runs


def test_in_its_full_mode_targets_can_come_down_but_stay_well_above_the_fees():
    desk = make()
    desk.settings.learn_targets_raise_only = False
    tl = desk.bots["targets"]
    for _ in range(10):
        trade(desk, "BTC-USD", 100.0, 100.2, 100.1)  # winners that barely moved
    t = tl.target("BTC-USD", 0.04)
    assert t >= 0.6 * 0.04 and t >= desk.settings.round_trip_cost("BTC-USD", 1.0) * desk.settings.flip_min_edge


def test_trade_reviewer_sizes_finders_by_their_record():
    desk = make()
    rv = desk.bots["reviewer"]
    for _ in range(8):
        trade(desk, "SOL-USD", 100.0, 103.0, 102.5, reason="volume snipe: SOL-USD trading 5x")
        trade(desk, "BTC-USD", 100.0, 100.5, 98.0, reason="pullback: BTC-USD retested")
    assert rv.size_mult("volume snipe: X") > 1.0 and rv.size_mult("pullback: Y") <= 0.85
    assert 0.5 <= rv.size_mult("pullback: Y") and rv.size_mult("volume snipe: X") <= 1.25
    assert rv.size_mult("momentum lead in") == 1.0  # strategy signals: no record yet


def test_sources_are_told_apart():
    assert [source_of(r) for r in ("snipe: X broke", "volume snipe: X", "flip: X", "dip buy: X", "pullback: X",
                                   "skim: X", "momentum lead in trending")] == \
        ["snipe", "volume_snipe", "flipper", "dip_buy", "pullback", "skim", "strategy"]


def test_what_they_learn_is_remembered_after_a_restart(tmp_path):
    db = str(tmp_path / "tz.db")
    desk = make(db)
    for _ in range(9):
        trade(desk, "SOL-USD", 100.0, 103.0, 102.0, reason="volume snipe: SOL")
    d2 = make(db)
    run(d2.start())
    assert len(d2.bots["targets"].runs["SOL-USD"]) == 9 and d2.bots["reviewer"].stats["volume_snipe"]["n"] == 9
    assert d2.state()["learned_targets"][0]["learned"] is True
    run(d2.shutdown())


def test_both_can_be_switched_off():
    desk = make()
    desk.settings.learn_targets = desk.settings.learn_sources = False
    for _ in range(10):
        trade(desk, "SOL-USD", 100.0, 103.0, 102.0, reason="volume snipe: SOL")
    assert desk.bots["targets"].target("SOL-USD", 0.04) == 0.04 and desk.bots["reviewer"].size_mult("volume snipe") == 1.0
