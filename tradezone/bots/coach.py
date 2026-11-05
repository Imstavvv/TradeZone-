"""Coach - makes the team learn from its own results, within safe limits.

The team rules every bot works by::

    💰 Make money        only take trades whose reward beats the risk and the fees
    🎯 Take trades       don't sit idle when good chances appear
    🌱 Grow              let winnings compound; size up slowly when doing well, down when not
    🧠 Learn the markets every closed trade teaches the team what works

What the Coach learns (and how carefully):

* Every closed trade is credited to the strategies that suggested it (by their share of the
  signal), to the coin, and to the market mood (regime) at entry.
* **Strategy trust:** a strategy that keeps winning gets up to 1.5x weight in the Quant's vote,
  one that keeps losing drops to 0.5x. Results are shrunk toward 50/50 until there are enough
  trades (a strategy needs about 15 trades before half its record counts), so a lucky streak
  can't take over.
* **Coins:** a coin that keeps losing goes on the bench for a while (no new trades). Coins with
  a good record get slightly larger positions (up to 1.2x), poor ones smaller (down to 0.8x).
* **Growth:** after a run of solid results the risk per trade rises up to 1.25x; after losses it
  falls to 0.6x. Position sizes already scale with the balance, so gains compound.
* **Taking trades:** if the team has been idle for hours while results are healthy, the
  conviction bar is lowered a little (never more than 0.05). If results are poor it's raised.

Nothing the Coach does can move the hard safety limits (stops, daily loss, drawdown, exposure,
fees filter). Learned state is saved, so learning continues across restarts.
"""

from __future__ import annotations

import time

from ..core.bus import Event
from ..core.models import Regime, now
from .base import Bot
from .quant import FAMILIES, STRATEGIES as ALL_STRATEGIES, WEIGHTS

TEAM_RULES = [
    {"icon": "💰", "rule": "Make money", "detail": "Only take trades whose reward beats the risk and the fees."},
    {"icon": "🎯", "rule": "Take trades", "detail": "Don't sit idle when good chances appear."},
    {"icon": "🌱", "rule": "Grow", "detail": "Let winnings compound - size up slowly when doing well, down when not."},
    {"icon": "🧠", "rule": "Learn the markets", "detail": "Every closed trade teaches the team what works."},
    {"icon": "🚀", "rule": "The goal: grow fast, lose little", "detail": "Grow the balance rapidly by compounding many quick, fee-paid wins (up or down markets, where shorting works) - and keep losses minimal: a small stop on every trade, half-size trades on a losing day, and the day's gains locked in once they're real."},
    {"icon": "💵", "rule": "A $1,000+ day", "detail": "Aim for a $1,000+ day with the money on hand - and no ceiling above it: winners keep running with a moving take-profit, and a great day never stops the bots. If a good day starts slipping, new trades get smaller instead. Never by betting bigger to force it."},
    {"icon": "🛡️", "rule": "Don't lose the money", "detail": "Protecting what you put in comes first: stops on every trade, and trading stops itself after a bad day or a 10% drop. Growing fast never means bigger bets than the limits allow."},
]
STRATEGIES = tuple(ALL_STRATEGIES)
NICE = {"momentum": "Momentum", "mean_reversion": "Buying dips", "breakout": "Breakouts", "macd": "MACD",
        "vwap": "VWAP", "trend": "50-bar trend", "roc": "Rate of change", "obv": "On-balance volume",
        "bollinger": "Bollinger Bands", "stochastic": "Stochastic"}
FAMILY_OF = {m: fam for fam, members in FAMILIES.items() for m in members}
CORE = ("momentum", "mean_reversion", "breakout")


def regime_weights(regime: str) -> dict[str, float]:
    try:
        return WEIGHTS[Regime(regime)]
    except ValueError:
        return WEIGHTS[Regime.UNKNOWN]


def _blank() -> dict:
    return {"n": 0.0, "wins": 0.0, "pnl": 0.0}


def shrunk_win_rate(s: dict) -> float:
    """Win rate with a 50/50 prior worth 4 trades, so a few results can't swing it far."""
    return (s["wins"] + 2) / (s["n"] + 4)


def confidence(s: dict, k: float = 15.0) -> float:
    return s["n"] / (s["n"] + k)


class CoachBot(Bot):
    name = "coach"
    title = "Coach"
    role = "Learning & Team Rules"
    mission = "🧠 Learn the markets - and keep everyone to the team rules"
    description = (
        "Learns from every closed trade which strategies, coins and market moods work, then "
        "nudges the team within safe limits: trusts winning strategies more, benches coins that "
        "keep losing, sizes up slowly when doing well, and stops the team sitting idle."
    )

    SAVE_EVERY_S = 120

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.stats: dict[str, dict[str, dict]] = {"strategy": {}, "symbol": {}, "regime": {}, "combo": {}}
        self.signals: dict[str, dict] = {}  # signal id -> entry context
        self.requests: dict[str, str] = {}  # order request id -> signal id
        self.open: dict[str, dict] = {}  # "mode|symbol" -> {ctx, pnl} for trades still open
        self.bench: dict[str, float] = {}  # symbol -> benched until
        self.lessons: list[dict] = []
        self.threshold_adj = 0.0
        self.last_entry_ts = now()
        self._last_adjust = 0.0
        self._last_save = time.monotonic()
        self.learned_trades = 0

    def subscriptions(self):
        return {"signal.proposed": self.on_signal, "order.approved": self.on_approved, "trade.log": self.on_trade}

    # ------------------------------------------------------------- recording
    def on_signal(self, ev: Event) -> None:
        sig = ev.payload
        if sig.kind.value != "entry" or sig.source != "quant":
            return
        quant = self.desk.bots.get("quant")
        sc = dict(quant.scores.get(sig.symbol, {})) if quant else {}
        self.signals[sig.id] = {"symbol": sig.symbol, "regime": sc.get("regime", "unknown"),
                                "parts": {k: float(sc.get(k, 0.0)) for k in STRATEGIES}, "strength": sig.strength}
        if len(self.signals) > 2000:
            for k in list(self.signals)[:1000]:
                self.signals.pop(k, None)

    def on_approved(self, ev: Event) -> None:
        req = ev.payload
        if not req.reduce_only and req.signal_id in self.signals:
            self.requests[req.id] = req.signal_id
            if len(self.requests) > 2000:  # keep memory flat
                for k in list(self.requests)[:1000]:
                    self.requests.pop(k, None)
            if len(self.requests) > 2000:
                for k in list(self.requests)[:1000]:
                    self.requests.pop(k, None)

    def _context_for_fill(self, t: dict) -> dict | None:
        exe = self.desk.bots.get("execution")
        order = exe.orders.get(t.get("order_id")) if exe else None
        sig_id = self.requests.get(order.request_id) if order else None
        return self.signals.get(sig_id) if sig_id else None

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        sym = t["symbol"]
        key = f"{t.get('mode', 'paper')}|{sym}"  # paper and live keep separate books
        if t["action"] == "flip" and (old := self.open.pop(key, None)) is not None and t.get("pnl") is not None:
            self.learn(sym, old["ctx"], old["pnl"] + t["pnl"])
        if t["action"] in ("open", "flip"):
            self.open[key] = {"ctx": self._context_for_fill(t), "pnl": 0.0}
            self.last_entry_ts = now()
            return
        rec = self.open.get(key)
        if rec is None or t.get("pnl") is None:
            return
        rec["pnl"] += t["pnl"]
        if abs(t.get("position_after") or 0.0) < 1e-12:
            self.open.pop(key, None)
            self.learn(sym, rec["ctx"], rec["pnl"])

    # ---------------------------------------------------------------- learning
    def _add(self, table: str, key: str, share: float, win: bool, pnl: float) -> None:
        s = self.stats[table].setdefault(key, _blank())
        s["n"] += share
        s["wins"] += share if win else 0.0
        s["pnl"] += share * pnl

    def learn(self, symbol: str, ctx: dict | None, pnl: float) -> None:
        win = pnl > 0
        regime = (ctx or {}).get("regime", "unknown")
        self._add("symbol", symbol, 1.0, win, pnl)
        self._add("regime", regime, 1.0, win, pnl)
        self._add("combo", f"{symbol}|{regime}", 1.0, win, pnl)
        if ctx:
            w = regime_weights(regime)
            # each strategy is credited by how strongly it spoke, within its family's weight in this market
            contrib = {k: abs(ctx["parts"].get(k, 0.0)) * w[FAMILY_OF[k]] for k in STRATEGIES}
            total = sum(contrib.values()) or 1.0
            for k, c in contrib.items():
                if c > 0:
                    self._add("strategy", k, c / total, win, pnl)
        self.learned_trades += 1
        self.bump("trades_learned")
        self._maybe_bench(symbol)
        self._write_lessons(symbol, regime, win, pnl)
        self.save()

    def _maybe_bench(self, symbol: str) -> None:
        s = self.stats["symbol"].get(symbol)
        if s and s["n"] >= 5 and shrunk_win_rate(s) < 0.38 and s["pnl"] < 0 and symbol not in self.bench:
            mins = self.settings.coach_bench_minutes
            self.bench[symbol] = now() + mins * 60
            self.lesson(f"{symbol} keeps losing ({s['wins']:.0f} wins in {s['n']:.0f}) - benched for {mins:.0f} min", "warning")

    def _write_lessons(self, symbol: str, regime: str, win: bool, pnl: float) -> None:
        # a plain-English note every few trades, once there's something worth saying
        if self.learned_trades % 3 and self.learned_trades > 3:
            return
        strat = {k: v for k, v in self.stats["strategy"].items() if v["n"] >= 3}
        if strat:
            best = max(strat, key=lambda k: shrunk_win_rate(strat[k]))
            b = strat[best]
            self.lesson(f"{NICE[best]} is working best so far: {b['wins']:.0f} wins in {b['n']:.0f} trades "
                        f"(trust x{self.strategy_mult(best):.2f})")
        combo = {k: v for k, v in self.stats["combo"].items() if v["n"] >= 3}
        if combo:
            top = max(combo, key=lambda k: (shrunk_win_rate(combo[k]), combo[k]["pnl"]))
            sym, reg = top.split("|")
            c = combo[top]
            if c["pnl"] > 0:
                self.lesson(f"{sym} does well when the market is {reg}: {c['wins']:.0f} of {c['n']:.0f} won")

    def lesson(self, text: str, level: str = "info") -> None:
        if self.lessons and self.lessons[-1]["text"] == text:
            return
        self.lessons.append({"ts": now(), "text": text, "level": level})
        self.lessons = self.lessons[-12:]
        self.act(f"Learned: {text}", level)

    # ----------------------------------------------------- what others ask for
    def strategy_mult(self, k: str) -> float:
        if not self.enabled or not self.settings.coach_learning:
            return 1.0
        s = self.stats["strategy"].get(k)
        if not s or s["n"] < 3:
            return 1.0
        return max(0.5, min(1.5, 1 + 2 * (shrunk_win_rate(s) - 0.5) * confidence(s)))

    def symbol_mult(self, symbol: str) -> float:
        if not self.enabled or not self.settings.coach_learning:
            return 1.0
        s = self.stats["symbol"].get(symbol)
        if not s or s["n"] < 3:
            return 1.0
        return max(0.8, min(1.2, 1 + (shrunk_win_rate(s) - 0.5) * confidence(s, 10.0)))

    def benched(self, symbol: str) -> float:
        """Seconds left on the bench (0 = free to trade)."""
        if not self.enabled or not self.settings.coach_learning:
            return 0.0
        until = self.bench.get(symbol, 0.0)
        left = until - now()
        if left <= 0 and symbol in self.bench:
            self.bench.pop(symbol, None)
            if (st := self.stats["symbol"].get(symbol)) is not None:
                for f in st:
                    st[f] /= 2
            self.lesson(f"{symbol} is back off the bench - giving it another chance")
        return max(0.0, left)

    def risk_mult(self) -> float:
        """Grow when doing well, shrink when not (applied on top of the normal risk per trade)."""
        if not self.enabled or not self.settings.coach_learning:
            return 1.0
        trades = self.portfolio.closed_trades[-30:]
        if len(trades) < 15:
            return 1.0
        won = sum(t.pnl for t in trades if t.pnl > 0)
        lost = -sum(t.pnl for t in trades if t.pnl <= 0)
        pf = won / lost if lost > 0 else 3.0
        if self.portfolio.drawdown >= self.settings.limits(self.desk.is_live).max_drawdown_pct * 0.5:
            return 0.6
        if pf >= 1.5:
            return 1.25
        if pf >= 1.2:
            return 1.1
        if pf < 0.8:
            return 0.6
        if pf < 1.0:
            return 0.8
        return 1.0

    def bar(self) -> float:
        """The conviction a new trade needs: the configured minimum, nudged by what was learned."""
        base = self.settings.flip_min_strength if self.quick else self.settings.risk.min_signal_strength
        if (b := self.settings.boost(self.desk.is_live)) is not None:  # takes a few more chances, not just bigger ones
            base -= b["bar_cut"]
        if not self.enabled or not self.settings.coach_learning:
            return base
        return round(max(min(base, 0.18 if self.quick else 0.30), base + self.threshold_adj), 3)

    # ----------------------------------------------------------------- cycle
    async def on_cycle(self) -> None:
        s = self.settings
        if s.coach_learning and self.enabled and now() - self._last_adjust > 3600:
            idle_h = (now() - self.last_entry_ts) / 3600
            rm = self.risk_mult()
            if idle_h >= s.coach_idle_hours and rm >= 1.0 and self.threshold_adj > -0.05:
                self.threshold_adj = round(max(-0.05, self.threshold_adj - 0.02), 3)
                self._last_adjust = now()
                self.lesson(f"No trades for {idle_h:.0f} h while results are healthy - lowering the bar a little")
            elif rm < 1.0 and self.threshold_adj < 0.10:
                self.threshold_adj = round(min(0.10, self.threshold_adj + 0.02), 3)
                self._last_adjust = now()
                self.lesson("Recent results are weak - only the strongest chances for now", "warning")
            elif rm >= 1.0 and self.threshold_adj > 0:
                self.threshold_adj = round(max(0.0, self.threshold_adj - 0.02), 3)
                self._last_adjust = now()
                self.lesson("Results have recovered - back to the normal bar")
        self.metrics.update({"trades_learned": self.learned_trades, "risk_level": self.risk_mult(),
                             "bar": self.bar(), "benched": sum(1 for k in list(self.bench) if self.benched(k))})
        if time.monotonic() - self._last_save > self.SAVE_EVERY_S:
            self.save()

    # ------------------------------------------------------------ persistence
    def snapshot(self) -> dict:
        return {"stats": self.stats, "bench": self.bench, "lessons": self.lessons, "open": self.open,
                "threshold_adj": self.threshold_adj, "learned_trades": self.learned_trades,
                "last_entry_ts": self.last_entry_ts}

    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("coach", self.snapshot())
            self._last_save = time.monotonic()

    def load(self) -> bool:
        store = getattr(self.desk, "store", None)
        data = store.load("coach") if store is not None else None
        if not data:
            return False
        self.stats = {**self.stats, **data.get("stats", {})}
        self.bench = {k: v for k, v in data.get("bench", {}).items() if v > now()}
        self.lessons = data.get("lessons", [])[-12:]
        self.open = data.get("open", {})
        self.threshold_adj = data.get("threshold_adj", 0.0)
        self.learned_trades = data.get("learned_trades", 0)
        self.last_entry_ts = data.get("last_entry_ts", now())
        return True

    def summary(self) -> dict:
        sym = sorted(self.stats["symbol"].items(), key=lambda kv: -kv[1]["n"])[:8]
        return {
            "rules": TEAM_RULES,
            "trades_learned": self.learned_trades,
            "strategies": [{"key": k, "name": NICE[k], "trust": round(self.strategy_mult(k), 2),
                            "trades": round(self.stats["strategy"].get(k, _blank())["n"], 1),
                            "win_rate": shrunk_win_rate(self.stats["strategy"].get(k, _blank()))} for k in STRATEGIES
                           if k in CORE or k in self.settings.strategies],
            "symbols": [{"symbol": k, "trades": round(v["n"]), "wins": round(v["wins"]), "pnl": v["pnl"],
                         "size": round(self.symbol_mult(k), 2), "benched_min": round(self.benched(k) / 60)} for k, v in sym],
            "benched": [{"symbol": k, "minutes": round(left / 60)} for k in list(self.bench)
                        if (left := self.benched(k)) > 0],
            "risk_level": self.risk_mult(),
            "bar": self.bar(),
            "bar_base": self.settings.risk.min_signal_strength,
            "lessons": self.lessons[::-1],
        }

