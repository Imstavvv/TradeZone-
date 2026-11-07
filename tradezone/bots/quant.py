"""Quant - runs an ensemble of strategies and proposes trade signals."""

from __future__ import annotations

import math
from collections import deque

from ..core.bus import Event
from ..core.models import Regime, Side, Signal, Snapshot
from ..core.util import is_closing
from .base import Bot

# strategy weights per regime
WEIGHTS: dict[Regime, dict[str, float]] = {
    Regime.TRENDING: {"momentum": 0.5, "breakout": 0.4, "mean_reversion": 0.1},
    Regime.RANGING: {"momentum": 0.2, "breakout": 0.1, "mean_reversion": 0.7},
    Regime.VOLATILE: {"momentum": 0.25, "breakout": 0.25, "mean_reversion": 0.2},
    Regime.UNKNOWN: {"momentum": 0.4, "breakout": 0.3, "mean_reversion": 0.3},
}


def momentum_score(s: Snapshot) -> float:
    ind = s.indicators
    scale = max(s.price * ind["volatility"], 1e-9)
    return math.tanh((ind["ema_fast"] - ind["ema_slow"]) / scale)


def mean_reversion_score(s: Snapshot) -> float:
    rsi = s.indicators["rsi"]
    if 35 <= rsi <= 65:
        return 0.0
    return max(-1.0, min(1.0, (50 - rsi) / 25))


def breakout_score(s: Snapshot) -> float:
    ind = s.indicators
    if s.price > ind["donchian_high"]:
        return 1.0
    if s.price < ind["donchian_low"]:
        return -1.0
    return 0.0


def _scale(s: Snapshot, k: float = 1.0) -> float:
    return max(s.price * s.indicators.get("volatility", 0.0) * k, s.price * 1e-6, 1e-12)


def macd_score(s: Snapshot) -> float | None:
    """MACD: the histogram (MACD line minus its signal line) - momentum turning up or down."""
    h = s.indicators.get("macd_hist")
    return None if h is None else math.tanh(h / _scale(s, 0.5))


def vwap_score(s: Snapshot) -> float | None:
    """VWAP: trading above the volume-weighted average price is strength, below it weakness."""
    v = s.indicators.get("vwap")
    return None if not v else math.tanh((s.price - v) / _scale(s, 2.0))


def trend_score(s: Snapshot) -> float | None:
    """Trend: the price against its 50-bar average."""
    m = s.indicators.get("sma50")
    return None if not m else math.tanh((s.price - m) / _scale(s, 3.0))


def roc_score(s: Snapshot) -> float | None:
    """Rate of change: the move over the last 10 bars, against what's normal for the coin."""
    r = s.indicators.get("roc10")
    vol = s.indicators.get("volatility", 0.0)
    return None if r is None or vol <= 0 else math.tanh(r / (vol * math.sqrt(10) * 1.5))


def obv_score(s: Snapshot) -> float | None:
    """On-balance volume: is volume flowing in (up bars) or out (down bars)?"""
    o = s.indicators.get("obv_slope")
    return None if o is None else math.tanh(o / 3.0)


def bollinger_score(s: Snapshot) -> float | None:
    """Bollinger Bands: below the lower band is cheap (buy the dip), above the upper band dear."""
    ind = s.indicators
    lo, hi = ind.get("bb_lower"), ind.get("bb_upper")
    if lo is None or hi is None or hi <= lo:
        return None
    half = (hi - lo) / 2
    if s.price < lo:
        return min(1.0, 0.5 + (lo - s.price) / half)
    if s.price > hi:
        return -min(1.0, 0.5 + (s.price - hi) / half)
    return 0.0


def stochastic_score(s: Snapshot) -> float | None:
    """Stochastic %K: near the bottom of the recent range is oversold, near the top overbought."""
    k = s.indicators.get("stoch_k")
    if k is None:
        return None
    if 20 <= k <= 80:
        return 0.0
    return max(-1.0, min(1.0, (50 - k) / 30))


def _atr(s: Snapshot) -> float:
    return max(s.indicators.get("atr10") or s.indicators.get("atr") or 0.0, s.price * 1e-6, 1e-12)


def adx_score(s: Snapshot) -> float | None:
    """ADX with +DI / -DI: a trend only counts when ADX says there is one (over 20); its direction from the DIs."""
    ind = s.indicators
    adx, p, m = ind.get("adx"), ind.get("pdi"), ind.get("mdi")
    if adx is None or p is None or m is None or adx < 20:
        return None if adx is None else 0.0
    return math.tanh((p - m) / 15) * min(1.0, 0.5 + (adx - 20) / 20)


def aroon_score(s: Snapshot) -> float | None:
    """Aroon (25): a recent new high (up near 100) against a recent new low."""
    u, d = s.indicators.get("aroon_up"), s.indicators.get("aroon_down")
    if u is None or d is None:
        return None
    x = (u - d) / 100
    return x if abs(x) >= 0.4 else 0.0


def psar_score(s: Snapshot) -> float | None:
    """Parabolic SAR: above the SAR is an uptrend (stronger the further above, in ATRs)."""
    sar, up = s.indicators.get("psar"), s.indicators.get("psar_up")
    if sar is None or up is None:
        return None
    return (1 if up else -1) * min(1.0, 0.3 + abs(s.price - sar) / (3 * _atr(s)))


def supertrend_score(s: Snapshot) -> float | None:
    """Supertrend (10, 3): its trend direction."""
    up = s.indicators.get("supertrend_up")
    return None if up is None else (0.6 if up else -0.6)


def ichimoku_score(s: Snapshot) -> float | None:
    """Ichimoku: Tenkan above Kijun (and the price above Kijun) is bullish."""
    t, k = s.indicators.get("tenkan"), s.indicators.get("kijun")
    if t is None or k is None:
        return None
    a = _atr(s)
    return 0.5 * math.tanh((t - k) / a) + 0.5 * math.tanh((s.price - k) / (2 * a))


def trix_score(s: Snapshot) -> float | None:
    """TRIX (15): the slope of a triple-smoothed average, against what's normal for the coin."""
    tx, vol = s.indicators.get("trix"), s.indicators.get("volatility", 0.0)
    return None if tx is None or vol <= 0 else math.tanh(tx / (0.1 * vol))


def cmf_score(s: Snapshot) -> float | None:
    """Chaikin Money Flow (20): closes near the bars' highs on volume is buying pressure."""
    c = s.indicators.get("cmf")
    return None if c is None else math.tanh(4 * c)


def cci_score(s: Snapshot) -> float | None:
    """CCI (20): under -100 is oversold (buy the dip), over +100 overbought."""
    c = s.indicators.get("cci")
    if c is None:
        return None
    return 0.0 if abs(c) < 100 else max(-1.0, min(1.0, -c / 200))


def williams_score(s: Snapshot) -> float | None:
    """Williams %R (14): under -80 is oversold, over -20 overbought."""
    r = s.indicators.get("williams_r")
    if r is None:
        return None
    if r < -80:
        return min(1.0, (-80 - r) / 20 + 0.3)
    if r > -20:
        return -min(1.0, (r + 20) / 20 + 0.3)
    return 0.0


def mfi_score(s: Snapshot) -> float | None:
    """Money Flow Index (14): RSI with volume - under 20 oversold, over 80 overbought."""
    m = s.indicators.get("mfi")
    if m is None:
        return None
    if m < 20:
        return min(1.0, (20 - m) / 20 + 0.3)
    if m > 80:
        return -min(1.0, (m - 80) / 20 + 0.3)
    return 0.0


def keltner_score(s: Snapshot) -> float | None:
    """Keltner Channels (20 EMA +/- 2 ATR): a close outside the channel is a breakout."""
    up, lo = s.indicators.get("keltner_upper"), s.indicators.get("keltner_lower")
    if up is None or lo is None:
        return None
    return 1.0 if s.price > up else -1.0 if s.price < lo else 0.0


MINOR_WEIGHT = 0.2  # a model weighted this little in a regime abstains instead of vetoing

#: every strategy the Quant runs (each one's record is kept by the Coach, which trusts the winners more)
STRATEGIES = {
    "momentum": momentum_score,
    "mean_reversion": mean_reversion_score,
    "breakout": breakout_score,
    "macd": macd_score,
    "vwap": vwap_score,
    "trend": trend_score,
    "roc": roc_score,
    "obv": obv_score,
    "bollinger": bollinger_score,
    "stochastic": stochastic_score,
    # the wider set (each off until it's on in settings.strategies)
    "adx": adx_score, "aroon": aroon_score, "psar": psar_score, "supertrend": supertrend_score,
    "ichimoku": ichimoku_score, "trix": trix_score, "cmf": cmf_score,
    "cci": cci_score, "williams": williams_score, "mfi": mfi_score, "keltner": keltner_score,
}
#: ...grouped into the three families that vote (so ten models don't give ten vetoes)
FAMILIES = {
    "momentum": ("momentum", "macd", "vwap", "trend", "roc", "obv", "adx", "aroon", "psar", "supertrend", "ichimoku",
                 "trix", "cmf"),
    "mean_reversion": ("mean_reversion", "bollinger", "stochastic", "cci", "williams", "mfi"),
    "breakout": ("breakout", "keltner"),
}


class QuantBot(Bot):
    name = "quant"
    title = "Quant"
    role = "Strategy & Signal Generation"
    mission = "🎯 Take trades - spot real chances and bring them to the team"
    description = (
        "Runs momentum, mean-reversion and breakout models, blends them using the "
        "Strategist's regime call, and proposes buy/sell signals with a conviction score."
    )

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.stance: dict[str, int] = {}  # -1, 0, +1 per symbol (hysteresis)
        self.scores: dict[str, dict[str, float]] = {}
        self.emitted: dict[str, int] = {}  # stance already turned into a signal
        self.blocked: dict[str, str] = {}  # last consensus failure per symbol
        self.last_snap: dict[str, Snapshot] = {}
        self._voting_meme = False
        self.sniped: dict[str, float] = {}  # meme coin -> breakout level already acted on
        self._bars_since: dict[str, int] = {}  # symbol -> bars since its last trade idea
        self._surge_wait: dict[str, int] = {}  # symbol -> bars until it may be volume-sniped again

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot, "market.tick": self.on_tick}

    # ---- sniping: act on a meme-coin breakout the moment it happens, not at the bar close ----
    def on_tick(self, ev: Event) -> None:
        t = ev.payload
        s = self.settings
        if not s.snipe or not s.is_meme(t.symbol) or (snap := self.last_snap.get(t.symbol)) is None:
            return
        level = snap.indicators["donchian_high"]
        if t.price < level * (1 + s.snipe_breakout_pct) or self.sniped.get(t.symbol) == level:
            return
        if not self.portfolio.position(t.symbol).is_flat or self.emitted.get(t.symbol) == 1:
            return
        self.sniped[t.symbol] = level  # one shot per breakout level
        mom = momentum_score(snap)
        strategist = self.desk.bots.get("strategist")
        regime = strategist.regime(t.symbol) if strategist and strategist.enabled else Regime.UNKNOWN
        # a breakout isn't a mean-reversion setup, and meme coins are always "volatile": those two
        # abstain; the breakout and momentum must agree and nobody may object
        votes = {"breakout": "yes", "momentum": "yes" if mom > 0.1 else "no" if mom < -0.1 else "abstain",
                 "mean_reversion": "abstain",
                 "strategist": "yes" if regime is Regime.TRENDING and mom > 0 else
                               "no" if regime is Regime.TRENDING else "abstain"}
        yes = sum(v == "yes" for v in votes.values())
        if "no" in votes.values() or yes < s.risk.consensus_min_yes:
            self.bump("snipes_passed")
            self.act(f"no snipe on {t.symbol}: breakout without momentum")
            return
        jump = t.price / level - 1
        self.stance[t.symbol] = self.emitted[t.symbol] = 1
        self.blocked.pop(t.symbol, None)
        sig = Signal(t.symbol, Side.BUY, round(min(1.0, 0.5 + 0.5 * mom), 3),
                     f"snipe: {t.symbol} broke {jump:+.1%} above its recent high; consensus {yes} yes",
                     self.name, votes=votes)
        self.publish("signal.proposed", sig)
        self.bump("snipes")
        self.act(f"SNIPE {t.symbol} @ {t.price:,.6g} (+{jump:.1%} through the high)")

    def _surge_snipe(self, snap: Snapshot, before: Snapshot | None) -> bool:
        """Volume snipe: a crypto or meme coin trading several times its usual volume while the price jumps up."""
        st, sym = self.settings, snap.symbol
        if self._surge_wait.get(sym, 0) > 0:
            self._surge_wait[sym] -= 1
            return False
        if not st.surge_snipe or not st.is_crypto(sym) or before is None or before.price <= 0:
            return False
        ind = snap.indicators
        vr = ind.get("volume_ratio")
        move = snap.price / before.price - 1
        if vr is None or vr < st.surge_volume_ratio or move < st.surge_min_move:
            return False
        if snap.price < ind.get("ema_fast", snap.price) or ind.get("rsi", 50) > st.surge_max_rsi:
            return False
        if not self.portfolio.position(sym).is_flat:
            return False
        mom = momentum_score(snap)
        votes = {"volume": "yes", "breakout": "yes" if snap.price >= ind.get("donchian_high", float("inf")) else "abstain",
                 "momentum": "yes" if mom > 0.1 else "no" if mom < -0.5 else "abstain"}
        self._surge_wait[sym] = st.surge_cooldown_bars
        if "no" in votes.values():
            self.bump("surges_passed")
            self.act(f"no volume snipe on {sym}: {vr:.1f}x volume but the trend is still down")
            return False
        self.stance[sym] = self.emitted[sym] = 1
        self.blocked.pop(sym, None)
        self._bars_since[sym] = 0
        strength = round(min(1.0, 0.55 + 0.05 * min(vr, 8.0) + 0.2 * max(mom, 0.0)), 3)
        self.publish("signal.proposed", Signal(
            sym, Side.BUY, strength, f"volume snipe: {sym} trading {vr:.1f}x its usual volume, +{move:.1%} this bar",
            self.name, votes=votes))
        self.bump("volume_snipes")
        self.act(f"VOLUME SNIPE {sym} @ {snap.price:,.6g}: {vr:.1f}x volume, +{move:.1%}")
        return True

    def _vb_record(self, s: Snapshot) -> tuple[float | None, float | None]:
        """Keep each coin's longer price and volume history (warm-up bars too); returns (prior high, prior avg volume)."""
        st = self.settings
        book = self.__dict__.setdefault("_vb", {})
        px, vol = book.setdefault(s.symbol, (deque(maxlen=st.vb_high_bars), deque(maxlen=st.vb_volume_bars)))
        hi = max(px) if len(px) >= st.vb_high_bars else None
        av = sum(vol) / len(vol) if len(vol) >= st.vb_volume_bars else None
        px.append(s.price)
        v = s.indicators.get("volume")
        if v is not None:
            vol.append(float(v))
        return hi, av

    def _vol_breakout(self, s: Snapshot, hi: float | None, av: float | None) -> bool:
        """Volume breakout: a new 4-hour high on clearly more volume than usual."""
        st, sym = self.settings, s.symbol
        wait = self.__dict__.setdefault("_vb_wait", {})
        if wait.get(sym, 0) > 0:
            wait[sym] -= 1
            return False
        if not st.vol_breakout or hi is None or not av or av <= 0:
            return False
        v = s.indicators.get("volume")
        if v is None or s.price <= hi or v < st.vb_volume_ratio * av:
            return False
        if not self.portfolio.position(sym).is_flat or self.desk.in_focus(sym) or self.settings.is_stack_only(sym):
            return False
        wait[sym] = st.vb_cooldown_bars
        ratio = v / av
        self.stance[sym] = self.emitted[sym] = 1
        self.blocked.pop(sym, None)
        self._bars_since[sym] = 0
        self.publish("signal.proposed", Signal(
            sym, Side.BUY, round(min(1.0, 0.55 + 0.05 * min(ratio, 6.0)), 3),
            f"volume breakout: {sym} at a new {st.vb_high_bars // 60}-hour high on {ratio:.1f}x its usual volume",
            self.name, votes={"breakout": "yes", "volume": "yes"}))
        self.bump("volume_breakouts")
        self.act(f"VOLUME BREAKOUT {sym} @ {s.price:,.6g}: new {st.vb_high_bars // 60} h high, {ratio:.1f}x volume")
        return True

    def on_snapshot(self, ev: Event) -> None:
        s: Snapshot = ev.payload
        hi, av = self._vb_record(s)
        if s.historical:
            return  # warm-up data: never trade on the past
        before = self.last_snap.get(s.symbol)
        self.last_snap[s.symbol] = s
        self.bump("checks")  # every bar looked at: shown as "chances checked"
        strategist = self.desk.bots.get("strategist")
        regime = strategist.regime(s.symbol) if strategist and strategist.enabled else Regime.UNKNOWN
        coach = self.desk.bots.get("coach")
        every = {k: v for k, fn in STRATEGIES.items() if self._on(k) and (v := fn(s)) is not None}
        parts, trust = self.families(every, coach)
        # 🧠 learn: a family whose strategies have been working counts for more
        base = dict(WEIGHTS[regime])
        if regime is Regime.RANGING:
            base["mean_reversion"] = self.settings.mr_ranging_weight
        weights = {k: w * trust[k] for k, w in base.items()}
        score = sum(weights[k] * v for k, v in parts.items()) / sum(weights.values())
        self.scores[s.symbol] = {**every, **{f"family_{k}": v for k, v in parts.items()},  # type: ignore[dict-item]
                                 "ensemble": score, "regime": regime.value}
        if self.settings.is_stack_only(s.symbol) and self.portfolio.position(s.symbol).is_flat:
            return  # a hold-stack-only coin: scored (the Stacker reads it), never a new trade
        if self._surge_snipe(s, before):  # (after the scores: the Coach learns from this bar's readings)
            return
        if self._vol_breakout(s, hi, av):
            return

        th = coach.bar() if coach is not None else self.settings.risk.min_signal_strength
        prev = self.stance.get(s.symbol, 0)
        new = prev
        if score >= th:
            new = 1
        elif score <= -th:
            new = -1
        elif abs(score) < th / 3:
            new = 0
        self.stance[s.symbol] = new
        self._bars_since[s.symbol] = self._bars_since.get(s.symbol, 0) + 1
        if new != prev:
            self.emitted.pop(s.symbol, None)
        if new != 0 and self.emitted.get(s.symbol) == new and self.quick \
                and self.portfolio.position(s.symbol).is_flat and self._bars_since[s.symbol] >= self.settings.reentry_bars:
            self.emitted.pop(s.symbol, None)  # real money: the trend still holds and the last trade is done - go again
        if new == 0 or self.emitted.get(s.symbol) == new:
            return
        side = Side.BUY if new > 0 else Side.SELL
        pos = self.portfolio.position(s.symbol)
        closing = is_closing(pos.qty, side)
        top = max(parts, key=lambda k: abs(parts[k] * weights[k]))
        reason = f"{top} lead in {regime.value} regime (score {score:+.2f})"
        votes: dict[str, str] = {}
        if not closing and side is Side.BUY and top == "mean_reversion" and (wait := self._dip_wait(s, before)):
            if self.blocked.get(s.symbol) != wait:
                self.blocked[s.symbol] = wait
                self.bump("dip_buys_waited")
                self.act(f"not buying the {s.symbol} dip yet: {wait}")
            return  # (the stance holds: re-checked on the next bar)
        if not closing and side is Side.BUY and (wait := self.buy_area_wait(self.settings.buy_area, s.price, s.indicators)):
            if self.blocked.get(s.symbol) != wait:
                self.blocked[s.symbol] = wait
                self.bump("buys_waited_for_area")
                self.act(f"not buying {s.symbol} yet: {wait}")
            return  # (the stance holds: re-checked on the next bar)
        if not closing:
            if self.desk.in_focus(s.symbol):
                return  # waits for a bigger balance (growth ladder): not proposed yet
            if side is Side.SELL and self.desk.short_block(s.symbol):
                self.emitted[s.symbol] = new
                return  # shorting isn't possible here: a bearish view with nothing to sell is a no-op
            if not pos.is_flat:
                return  # already positioned this way
            self._voting_meme = self.settings.is_meme(s.symbol)
            votes = self.consensus(new, parts, regime)
            yes = sum(v == "yes" for v in votes.values())
            against = [k for k, v in votes.items() if v == "no"]
            share = self.settings.risk.consensus_min_share
            need = max(1, math.ceil(share * len(votes) - 1e-9)) if share > 0 else self.settings.risk.consensus_min_yes
            if against or yes < need:
                why = f"{', '.join(against)} against" if against else f"only {yes}/{need} agree"
                if self.blocked.get(s.symbol) != why:  # log each distinct disagreement once
                    self.blocked[s.symbol] = why
                    self.bump("consensus_blocked")
                    self.act(f"no consensus to {side.value} {s.symbol}: {why}")
                return  # keep re-checking on later bars while the stance holds
            reason += f"; consensus {yes} yes / {sum(v == 'abstain' for v in votes.values())} abstain"
        self.blocked.pop(s.symbol, None)
        self.emitted[s.symbol] = new
        self._bars_since[s.symbol] = 0
        sig = Signal(s.symbol, side, round(min(1.0, abs(score)), 3), reason, self.name, votes=votes)
        self.publish("signal.proposed", sig)
        self.bump("signals")
        self.act(f"proposed {side.value.upper()} {s.symbol} @ {s.price:,.2f} ({reason})")

    @staticmethod
    def buy_area_wait(mode: str, price: float, ind: dict) -> str | None:
        """Why the price isn't in the best buy area yet (None: it is, or there's no view to judge it by)."""
        if mode == "range" and (rp := ind.get("tf_1h_range_pos")) is not None and rp > 0.5:
            return f"it's in the top half of its hourly range ({rp:.0%}) - waiting for a better price"
        if mode == "value":
            value = min(v for v in (ind.get("bb_mid"), ind.get("vwap")) if v) if (ind.get("bb_mid") or ind.get("vwap")) else None
            if value and price > value:
                return f"it's above its short-term value ({value:,.6g}) - waiting for it to come back"
        if mode == "support":
            lo, hi = ind.get("donchian_low"), ind.get("donchian_high")
            if lo and hi and hi > lo and (price - lo) / (hi - lo) > 0.4:
                return "it's well above its recent low - waiting for a buy nearer support"
        if mode == "cool" and (ind.get("tf_1h_rsi", 50) > 60 or ind.get("tf_15m_rsi", 50) > 65):
            return "it's overbought on the higher timeframes - waiting for it to cool"
        return None

    def _dip_wait(self, s: Snapshot, before: Snapshot | None) -> str | None:
        """Why a mean-reversion dip buy should wait, or None to go ahead."""
        mode = self.settings.mr_buy_filter
        if mode == "off":
            return None
        ind = s.indicators
        if mode in ("trend", "both") and s.price < ind.get("sma50", 0.0):
            return "it's below its 50-bar average - a dip in a downtrend"
        if mode in ("turn", "both"):
            if before is None or ind.get("rsi", 50) <= before.indicators.get("rsi", 50) or s.price <= before.price:
                return "still falling - waiting for it to turn up"
        return None

    def _on(self, name: str) -> bool:
        return name in ("momentum", "mean_reversion", "breakout") or name in self.settings.strategies

    @staticmethod
    def families(every: dict[str, float], coach=None) -> tuple[dict[str, float], dict[str, float]]:
        """Each family's view (the average of its strategies with an opinion, each counted by how well it has
        been doing) and how much the family is trusted (its strategies' average track record)."""
        out, trust = {}, {}
        for fam, members in FAMILIES.items():
            # a strategy with no opinion right now (exactly 0: inside its bands, no breakout) abstains
            vals = [(every[m], coach.strategy_mult(m) if coach is not None else 1.0) for m in members
                    if m in every and every[m] != 0.0]
            w = sum(x for _, x in vals)
            out[fam] = sum(v * x for v, x in vals) / w if w > 0 else 0.0
            trust[fam] = w / len(vals) if vals else (coach.strategy_mult(members[0]) if coach is not None else 1.0)
        return out, trust

    def consensus(self, direction: int, parts: dict[str, float], regime: Regime) -> dict[str, str]:
        """Each voter says yes / no / abstain on opening a position in ``direction``."""
        def vote(x: float, dead: float = 0.1) -> str:
            x *= direction
            return "yes" if x > dead else "no" if x < -dead else "abstain"

        votes = {k: vote(v) for k, v in parts.items()}
        # A model that barely counts in this kind of market doesn't get a veto: in a trend the
        # "buy the dip" model always objects to buying strength, and in a sideways market the trend
        # models always object to buying a dip. They abstain there instead of blocking every trade.
        weights = WEIGHTS[regime]
        for k, v in list(votes.items()):
            if v == "no" and weights.get(k, 1.0) <= MINOR_WEIGHT:
                votes[k] = "abstain"
        strategist = self.desk.bots.get("strategist")
        if strategist is None or not strategist.enabled or regime is Regime.UNKNOWN:
            votes["strategist"] = "abstain"
        elif regime is Regime.VOLATILE:
            # no new risk in a volatility storm - except on meme coins, where "volatile" is normal
            votes["strategist"] = "abstain" if self._voting_meme else "no"
        elif regime is Regime.TRENDING:
            votes["strategist"] = "yes" if parts["momentum"] * direction > 0 else "no"
        else:  # ranging: only buying weakness / selling strength makes sense
            votes["strategist"] = "yes" if parts["mean_reversion"] * direction > 0 else "abstain"
        return votes
