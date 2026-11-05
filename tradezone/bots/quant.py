"""Quant - runs an ensemble of strategies and proposes trade signals."""

from __future__ import annotations

import math

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


MINOR_WEIGHT = 0.2  # a model weighted this little in a regime abstains instead of vetoing

STRATEGIES = {
    "momentum": momentum_score,
    "mean_reversion": mean_reversion_score,
    "breakout": breakout_score,
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

    def on_snapshot(self, ev: Event) -> None:
        s: Snapshot = ev.payload
        if s.historical:
            return  # warm-up data: never trade on the past
        before = self.last_snap.get(s.symbol)
        self.last_snap[s.symbol] = s
        if self._surge_snipe(s, before):
            return
        self.bump("checks")  # every bar looked at: shown as "chances checked"
        strategist = self.desk.bots.get("strategist")
        regime = strategist.regime(s.symbol) if strategist and strategist.enabled else Regime.UNKNOWN
        coach = self.desk.bots.get("coach")
        weights = WEIGHTS[regime]
        if coach is not None:  # 🧠 learn: trust the strategies that have been working
            weights = {k: w * coach.strategy_mult(k) for k, w in weights.items()}
        parts = {k: fn(s) for k, fn in STRATEGIES.items()}
        score = sum(weights[k] * v for k, v in parts.items()) / sum(weights.values())
        self.scores[s.symbol] = {**parts, "ensemble": score, "regime": regime.value}  # type: ignore[dict-item]

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
            need = self.settings.risk.consensus_min_yes
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
