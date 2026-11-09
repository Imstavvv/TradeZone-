import pytest

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal


@pytest.fixture
def settings():
    s = Settings(never_sell_at_loss=False, feed="sim", wide_stop=False, accumulate=False,
                 loser_cap=0.0)  # most tests check the normal plan (own tests cover these)
    s.risk.symbol_cooldown_s = 0
    s.risk.max_orders_per_minute = 10**6
    return s


@pytest.fixture
def desk(settings):
    return TradingDesk(settings, journal=Journal(":memory:"))


#: the stops / targets before "stop-loss at most -7%, take-profit +15%": tests of a mechanism (dip buys, runners,
#: profit sales at the ask, the moving target, ...) were written around these numbers and keep testing it with them
OLD_PLAN = {"wide_stop_pct": 0.20, "max_loss_pct": 0.20, "patient_meme_stop_pct": 0.12, "stop_loss_pct": 0.08,
            "flip_major_target_pct": 0.04, "flip_meme_target_pct": 0.10, "major_take_profit_pct": 0.06,
            "tp_step_pct": 0.10, "tp_step_before_target": True}


#: features switched off after failing on unseen data: tests of how they work turn them back on
KILLED_ON = {"trailing_stop": True, "live_quick_flips": True, "flipper": True, "patient_exits": True, "coach_learning": True,
             "surge_snipe": True, "runners": True, "dip_buy": True, "learn_sources": True, "snipe": True, "pullback": True,
             "strategies": ["macd", "obv", "mfi"], "bots_off": []}


@pytest.fixture(autouse=True)
def _with_killed(request, monkeypatch):
    if request.node.get_closest_marker("with_killed") is None:
        return
    from tradezone.config import Settings
    real = Settings.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        for k, v in KILLED_ON.items():
            if k not in kw:
                setattr(self, k, list(v) if isinstance(v, list) else v)
    monkeypatch.setattr(Settings, "__init__", init)


@pytest.fixture(autouse=True)
def _old_plan(request, monkeypatch):
    if request.node.get_closest_marker("old_plan") is None:
        return
    from tradezone.config import Settings
    real = Settings.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        for k, v in OLD_PLAN.items():
            if k not in kw:
                setattr(self, k, v)
    monkeypatch.setattr(Settings, "__init__", init)


@pytest.fixture(autouse=True)
def _no_entry_filters(request, monkeypatch):
    """Tests prime a coin with neutral readings (RSI 50, no trend strength): the losing-trade entry filters (RSI 60+,
    ADX 15+, the timeframes not leaning down, no skims) would block their buys. They're off in tests, except tests
    marked entry_filters - the filters' own."""
    if request.node.get_closest_marker("entry_filters") is not None:
        return
    from tradezone.config import Settings
    real = Settings.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        if "entry_min_rsi" not in kw:
            self.entry_min_rsi = 0.0
        if "entry_min_adx" not in kw:
            self.entry_min_adx = 0.0
        if "mtf_filter" not in kw:
            self.mtf_filter = "off"
        if "max_skims" not in kw:
            self.max_skims = 2
    monkeypatch.setattr(Settings, "__init__", init)


@pytest.fixture(autouse=True)
def _not_kraken_only(request, monkeypatch):
    """Tests written around the old fallback (no Kraken key: Coinbase / Alpaca prices and fees)."""
    if request.node.get_closest_marker("not_kraken_only") is None:
        return
    from tradezone.config import Settings
    real = Settings.__init__

    def init(self, *a, **kw):
        kw.setdefault("kraken_only", False)
        real(self, *a, **kw)
    monkeypatch.setattr(Settings, "__init__", init)
    real_env = Settings.from_env.__func__

    def from_env(cls):
        s = real_env(cls)
        s.kraken_only = False
        return s
    monkeypatch.setattr(Settings, "from_env", classmethod(from_env))


@pytest.fixture(autouse=True)
def _old_entry(request, monkeypatch):
    """Tests written around the old buy bar (conviction 0.35, 2 voting families) - before 0.5 / 3 won its replays."""
    if request.node.get_closest_marker("old_entry") is None:
        return
    from tradezone.config import Settings
    real = Settings.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        self.risk.min_signal_strength, self.risk.consensus_min_yes = 0.35, 2
    monkeypatch.setattr(Settings, "__init__", init)


@pytest.fixture(autouse=True)
def _tp15(request, monkeypatch):
    """Tests written around the +15% take-profit (before +6% won its replays): the old target for them."""
    if request.node.get_closest_marker("tp15") is None:
        return
    from tradezone.config import Settings
    real = Settings.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        old = OLD_PLAN if request.node.get_closest_marker("old_plan") is not None else {}
        for k in ("take_profit_pct", "major_take_profit_pct", "tp_step_pct"):
            if k not in kw and k not in old:  # (the old plan's own values win where it sets them)
                setattr(self, k, 0.15)
    monkeypatch.setattr(Settings, "__init__", init)


@pytest.fixture(autouse=True)
def _no_history_pause(monkeypatch):
    """The Trend Scout spaces out history requests (the exchange's rate limit); tests don't need to wait."""
    import tradezone.bots.trends as trends
    waited = []

    async def fake(s):
        waited.append(s)
    monkeypatch.setattr(trends, "_pause", fake)
    return waited
