"""Risk Manager - the gatekeeper between ideas and orders."""

from __future__ import annotations

import calendar
import time
from collections import deque

from ..core.bus import Event
from ..core.models import (
    Alert,
    OrderRequest,
    Regime,
    Side,
    Signal,
    SignalKind,
    Snapshot,
    now,
)
from ..core.stops import StopRejected, plan_stop
from ..core.util import is_closing, round_qty
from .advisors import buy_area, falling_buy_ok, falling_coin, turned_up
from .base import Bot


def s0_meme_only(settings) -> bool:
    return bool(getattr(settings, "meme_only", False))

class RiskBot(Bot):
    name = "risk"
    title = "Risk Manager"
    role = "Risk Control & Position Sizing"
    mission = "💰 Make money - only trades whose reward beats the risk and fees"
    description = (
        "Vets every signal against exposure, concentration, cooldown and rate limits, sizes "
        "positions by volatility, and trips the kill switch on excessive loss or drawdown."
    )
    critical = True

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self._rotation: dict[str, tuple] = {}  # buy waiting for a partial sale to fill -> (signal, when)
        self._rotated: set[str] = set()
        self.snapshots: dict[str, Snapshot] = {}
        self.long_px: dict[str, deque[float]] = {}  # (the falling-market guard's longer price history)
        self.order_times: deque[float] = deque()
        self.last_trade: dict[str, float] = {}
        self.seen_signals: set[str] = set()
        self.strength_by_signal: dict[str, float] = {}  # for the Executive's defensive stance
        self._peak_key: tuple | None = None  # (mode, day, book) the day's best equity belongs to
        self.day_peak = 0.0

    # ---- the goal: grow fast, lose little --------------------------------------------
    def _track_day_peak(self) -> float:
        p = self.portfolio
        key = (self.desk.mode, self.desk._day, id(p))
        if key != self._peak_key:
            self._peak_key, self.day_peak = key, max(p.day_start_equity, p.equity)
        self.day_peak = max(self.day_peak, p.equity)
        return self.day_peak

    def day_guard(self) -> dict:
        """Today's gains lock and the losing-day brake."""
        p, s = self.portfolio, self.settings
        start, peak = p.day_start_equity, self._track_day_peak()
        best = peak - start
        b = s.boost(self.desk.is_live)
        trigger = b["day_lock_trigger"] if b else s.day_lock_trigger_pct
        giveback = b["day_giveback"] if b else s.day_giveback_pct
        locked = start > 0 and best >= trigger * start and p.equity <= start + best * (1 - giveback)
        goal = self.desk.daily_goal()
        goal_day = goal > 0 and best >= goal  # a goal day: protect it more tightly
        if goal_day and p.equity <= start + best * (1 - s.daily_goal_giveback):
            locked, giveback = True, s.daily_goal_giveback
        lim = s.limits(self.desk.is_live).max_daily_loss_pct
        if self.desk.is_live:
            lim = min(lim, s.live_limits()[0])
        return {"locked": bool(locked), "goal_day": bool(goal_day), "best_today": best, "best_today_pct": best / start if start > 0 else 0.0,
                "brake": p.daily_pnl_pct <= -lim / 2, "keep_at_least": start + best * (1 - giveback)}

    def subscriptions(self):
        return {
            "market.snapshot": self.on_snapshot,
            "signal.proposed": self.on_signal,
            "signal.reviewed": self.on_reviewed,
        }

    def on_snapshot(self, ev: Event) -> None:
        self.snapshots[ev.payload.symbol] = ev.payload
        # (kept whether or not the falling-market guard is on, so switching it on works at once)
        st = self.settings
        px = self.long_px.get(ev.payload.symbol)
        if px is None or px.maxlen != st.falling_window + st.falling_slope_bars:
            px = self.long_px[ev.payload.symbol] = deque(px or (), maxlen=st.falling_window + st.falling_slope_bars)
        px.append(ev.payload.price)

    def _falling(self, symbol: str) -> bool | None:
        """True when the coin is below its long average and that average is lower than it was; None: too little history."""
        st = self.settings
        px = self.long_px.get(symbol)
        w, k = st.falling_window, st.falling_slope_bars
        if not px or len(px) < w + 1:  # judged on too little history it did worse: wait for the full window
            return None
        xs = list(px)
        lag = min(k, len(xs) - w)
        avg_now = sum(xs[-w:]) / w
        avg_then = sum(xs[-w - lag:len(xs) - lag]) / w
        return xs[-1] < avg_now * (1 - st.falling_min_drop) and avg_now < avg_then

    def _trend(self, symbol: str) -> str | None:
        """'up' (above a rising 4-hour average), 'down' (below a falling one), 'flat', or None (too little history)."""
        st = self.settings
        px = self.long_px.get(symbol)
        w, k = st.falling_window, st.falling_slope_bars
        if not px or len(px) < w + 1:
            return None
        xs = list(px)
        lag = min(k, len(xs) - w)
        now_avg = sum(xs[-w:]) / w
        then_avg = sum(xs[-w - lag:len(xs) - lag]) / w
        if xs[-1] > now_avg and now_avg > then_avg:
            return "up"
        if xs[-1] < now_avg and now_avg < then_avg:
            return "down"
        return "flat"

    def market_down_block(self) -> str | None:
        """Most of the market in an hourly downtrend (market_down_share): no new buys until it turns."""
        share = self.settings.market_down_share
        if share <= 0:
            return None
        trends = [x.indicators.get("tf_1h_trend") for x in self.snapshots.values()]
        trends = [t for t in trends if t is not None]
        if len(trends) < 3:
            return None
        down = sum(t < 0 for t in trends) / len(trends)
        if down > share:
            return f"most of the market is falling on the hourly chart ({down:.0%} of coins) - no new buys until it turns"
        return None

    def mtf_block(self, symbol: str) -> str | None:
        """The higher timeframes say no (mtf_filter): the hourly / 4-hour trend is down, or more timeframes fall."""
        mode = self.settings.mtf_filter
        snap = self.snapshots.get(symbol)
        if mode == "off" or snap is None:
            return None
        ind = snap.indicators
        if mode in ("1h", "4h") and ind.get(f"tf_{mode}_trend", 0) < 0:
            return f"{symbol}'s {mode} trend is down - no new buys until the higher timeframe turns"
        if mode == "align" and ind.get("mtf_ready", 0) >= 2 and ind.get("mtf_score", 0) < 0:
            return f"{symbol}: more timeframes falling than rising ({ind.get('mtf_down')} vs {ind.get('mtf_up')}) - not buying"
        return None

    def falling_block(self, symbol: str) -> str | None:
        st = self.settings
        if st.falling_guard == "coin" and self._falling(symbol):
            return f"{symbol} is in a downtrend (below its long average, which is falling) - no new buys until it turns"
        if st.falling_guard == "market":
            votes = [f for f in (self._falling(s) for s in self.long_px) if f is not None]
            if votes and sum(votes) / len(votes) >= st.falling_market_share:
                return f"the market is falling ({sum(votes)} of {len(votes)} coins in a downtrend) - no new buys until it turns"
        return None

    # ------------------------------------------------------------------
    def reject(self, sig: Signal, reason: str) -> None:
        self.bump("rejected")
        self.publish("signal.rejected", {"signal": sig.to_dict(), "reason": reason})
        self.act(f"REJECTED {sig.side.value} {sig.symbol}: {reason}", "warning")

    def approve(self, req: OrderRequest) -> None:
        self.bump("approved")
        self.order_times.append(now())
        self.last_trade[req.symbol] = now()
        self.publish("order.approved", req)
        tag = " (reduce-only)" if req.reduce_only else ""
        self.act(f"APPROVED {req.side.value} {req.qty:g} {req.symbol}{tag}")

    def _rate_limited(self) -> bool:
        cutoff = now() - 60
        while self.order_times and self.order_times[0] < cutoff:
            self.order_times.popleft()
        return len(self.order_times) >= self.settings.risk.max_orders_per_minute

    def _round_qty(self, qty: float, price: float) -> float:
        return round_qty(qty, price, self.settings.risk.fractional)

    def on_signal(self, ev: Event) -> None:
        sig: Signal = ev.payload
        grok = self.desk.bots.get("grok")
        if grok is not None and grok.gates(sig):
            return  # waits for Grok's verdict (signal.reviewed)
        self.evaluate(sig)

    def on_reviewed(self, ev: Event) -> None:
        r = ev.payload
        sig: Signal = r["signal"]
        if not r["approved"]:
            return self.reject(sig, f"Grok veto: {r['rationale']}")
        # blend the model's conviction with Grok's confidence
        sig.strength = round((sig.strength + r["confidence"]) / 2, 3)
        self.evaluate(sig)

    def evaluate(self, sig: Signal) -> None:
        if sig.id in self.seen_signals:
            return self.reject(sig, "duplicate signal id")
        self.seen_signals.add(sig.id)
        if len(self.seen_signals) > 50_000:
            self.seen_signals.clear()
        limits = self.settings.limits(self.desk.is_live)  # Aggressive (real money) allows bigger bets
        pos = self.portfolio.position(sig.symbol)
        price = self.desk.prices.get(sig.symbol)
        if not price:
            return self.reject(sig, "no market price")

        closing = is_closing(pos.qty, sig.side)
        if sig.kind is SignalKind.EXIT or closing:
            if pos.is_flat:
                return self.reject(sig, "nothing to close")
            if not closing:
                return self.reject(sig, "exit signal on wrong side")
            pm = self.desk.bots.get("portfolio")
            if sig.kind is SignalKind.ENTRY and pm is not None and sig.symbol in pm.scaled_out \
                    and self.settings.runners and self.settings.stop_method == "percent":
                return self.reject(sig, "holding the runner - its trailing stop decides the exit")
            if sig.kind is SignalKind.ENTRY and self.settings.exit_only_in_profit:
                # discretionary (strategy) exit: only to bank a net profit; losers are left to the stop
                net = self.settings.net_if_sold(pos, price)  # both fees counted
                if net <= 0:
                    return self.reject(sig, f"strategy exit skipped: not in profit ({net:+,.2f}); stop-loss still protects")
                if net < self.desk.min_take():
                    return self.reject(sig, f"strategy exit skipped: +{net:,.2f} isn't worth its share of the "
                                            f"~${self.desk.transfer_fee():,.2f} transfer fee yet")
                fees = self.settings.round_trip_cost(sig.symbol, pos.avg_price) * abs(pos.qty)
                if net < self.settings.strategy_exit_min_cost * fees:  # don't sell a winner for crumbs: let it run
                    return self.reject(sig, f"strategy exit skipped: +{net:,.2f} is under 2x the fees - letting the "
                                            "winner run to its target (its stop protects it)")
            qty = abs(pos.qty)
            if sig.fraction < 1.0:  # a partial take is profit-taking: never let it turn into selling everything
                least = max(self.desk.broker.min_qty(sig.symbol, price), 1.0 / price)  # the venue's smallest sale
                part = max(self._round_qty(qty * sig.fraction, price), least * 1.02)  # at least the minimum
                if qty - part < least:  # the rest would be unsellable dust: keep the whole trade running instead
                    return self.reject(sig, "profit slice skipped: too small to split on this venue - the whole trade "
                                            "keeps running (its stop protects it)")
                qty = part
            # the US day-trade rule: a stock bought today waits for the next trading day past the bots' share
            # (only YOUR forced sales may use the day trade kept back - an automatic emergency stop waits too)
            yours = sig.force and not (sig.reason.startswith("kill switch:") and "manual kill" not in sig.reason)
            if (why := self.desk.day_trade_block(sig.symbol, yours)):
                return self.reject(sig, why)
            # reduce-only exits are always allowed: they lower risk
            if self.settings.never_sell_at_loss and not sig.force and not sig.emergency \
                    and self.settings.net_if_sold(pos, price) < 0 \
                    and not self._past_floor(pos, price):
                return self.reject(sig, "held: it's below break-even after fees, and the bots never sell at a loss")
            # profit-taking (and a winner's change-of-mind exit) may wait at the ask for the maker fee
            calm = sig.calm or sig.kind is SignalKind.ENTRY
            if sig.calm and not self.settings.beats_fees(pos, price):  # stops and cuts are never held back
                net, fees = self.settings.net_if_sold(pos, price), self.settings.fees_if_sold(pos, price)
                return self.reject(sig, f"profit sale skipped: +{net:,.2f} after fees doesn't beat the "
                                        f"{fees:,.2f} the trade pays in fees yet - it keeps running (its stop protects it)")
            return self.approve(
                OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, reduce_only=True, calm=calm)
            )

        # ---- new risk from here on ----
        if self.desk.halted:
            return self.reject(sig, f"kill switch active ({self.desk.halt_reason})")
        if self.desk.paused:
            return self.reject(sig, "trading paused")
        if sig.side is Side.BUY and (rg := self.desk.bots.get("rugguard")) is not None and rg.enabled \
                and (why := rg.banned(sig.symbol)):
            return self.reject(sig, why)
        if sig.side is Side.BUY and s0_meme_only(self.settings) and not self.settings.is_meme(sig.symbol):
            return self.reject(sig, f"the Meme desk trades meme coins only - {sig.symbol} isn't one")
        if sig.side is Side.BUY and not sig.force and getattr(self.settings, "falling_coin_turn", False) \
                and sig.kind is SignalKind.ENTRY and falling_coin(snap1 := self.snapshots.get(sig.symbol)) \
                and not turned_up(snap1):
            return self.reject(sig, f"{sig.symbol} is still falling with no turnaround yet - waits for it to turn up")
        falling_mult = 1.0
        if sig.side is Side.BUY and not sig.force and (why := self.market_down_block()):
            ok, note = falling_buy_ok(self.settings, self.snapshots.get(sig.symbol))
            if not ok:
                return self.reject(sig, why)
            falling_mult = 0.7  # (bought in a falling market: smaller)
            self.bump("falling_buys_allowed")
            sig.reason = f"{sig.reason}; {note}"
        if sig.side is Side.BUY and not sig.force and (why := self.mtf_block(sig.symbol)):
            return self.reject(sig, why)
        if sig.side is Side.BUY and not sig.force and (why := self.falling_block(sig.symbol)):
            return self.reject(sig, why)  # (dip adds and buy-backs too: no catching a falling knife)
        rising_pass = sig.side is Side.BUY and self.settings.trend_split_rising_past_cap and self._trend(sig.symbol) == "up"
        if sig.side is Side.BUY and self.settings.trend_split_falling_size <= 0 and self._trend(sig.symbol) == "down":
            return self.reject(sig, f"{sig.symbol} is in a clear downtrend - no new buys of it until it turns")
        if sig.side is Side.BUY and not rising_pass and (why := self.desk.underwater_block()):
            return self.reject(sig, why)  # (before the Dip Buyer's adds: averaging down counts too)
        if sig.qty and sig.source == "flipper" and sig.side is Side.BUY and pos.qty > 0:
            return self._buy_back(sig, price)  # the Flipper buying back a slice it sold higher
        if sig.qty and sig.source == "dipbuyer" and sig.side is Side.BUY and pos.qty > 0:
            return self._dip_add(sig, pos, price)  # buying more of a held loser on a further dip
        if not pos.is_flat:
            return self.reject(sig, "already positioned in this direction")
        if self.settings.is_stack_only(sig.symbol):
            return self.reject(sig, f"{sig.symbol} is a hold-stack coin only: the Stacker buys it slowly, "
                                    "the trading bots don't trade it")
        guard = self.day_guard()
        if guard["locked"] and self.settings.day_lock_stops:  # (optional) a good day isn't handed back
            return self.reject(sig, f"locking in today's gains: up {guard['best_today_pct']:.1%} at best and too much "
                                    "of it given back - no new trades until tomorrow")
        if sig.side is Side.SELL and (why := self.desk.short_block(sig.symbol)):
            return self.reject(sig, f"no short: {why}")
        s0 = self.settings
        news = self.desk.bots.get("news")
        if news is not None:  # 📰 breaking news: don't buy into a hack
            if (why := news.veto(sig.symbol, sig.side.sign)) is not None:
                return self.reject(sig, why)
            sig.strength = round(max(0.0, min(1.0, sig.strength + news.conviction_nudge(sig.symbol, sig.side.sign))), 3)
        trends = self.desk.bots.get("trends")
        if trends is not None and (boost := trends.nudge(sig.symbol, sig.side.sign)):  # 🔥 trending right now
            sig.strength = round(min(1.0, sig.strength + boost), 3)
        # 🧑‍🤝‍🧑 the advisors: any may veto; nudges add up (+/-0.15 at most); size factors multiply (0.4x - 1.5x)
        nudge, advice_mult = 0.0, 1.0
        snap0 = self.snapshots.get(sig.symbol)
        for adv in self.desk.advisors:
            if not adv.enabled:
                continue
            veto, ds, sm = adv.advise(sig, snap0)
            if veto:
                self.bump("rejected_by_advisors")
                return self.reject(sig, f"{adv.title}: {veto}")
            nudge, advice_mult = nudge + ds, advice_mult * sm
        focus_mult = 1.0
        if sig.side is Side.BUY and sig.kind is SignalKind.ENTRY and s0.area_boost > 1.0 \
                and buy_area(snap_a := self.snapshots.get(sig.symbol)) \
                and not (s0.area_boost_uptrend_only and falling_coin(snap_a)):  # (a dip in an uptrend, not a fall)
            nudge += 0.05
            focus_mult *= s0.area_boost  # (a buy in a good buy area: more money where the return is best)
            self.bump("area_boosts")
        if sig.side is Side.BUY and sig.kind is SignalKind.ENTRY and s0.small_focus != "off":
            if s0.is_focus_coin(sig.symbol):  # (small market-cap and new coins first)
                nudge += s0.small_focus_nudge
                focus_mult *= s0.small_focus_size if s0.small_focus in ("size", "only") else 1.0
                self.bump("focus_buys")
            elif s0.small_focus == "only" and s0.is_crypto(sig.symbol):
                return self.reject(sig, f"focus on small and new coins: {sig.symbol} is a large / mid-size coin")
        if sig.side is Side.BUY and s0.dex_flips and s0.is_dex(sig.symbol):
            nudge += s0.dex_focus_nudge  # (DEX coins are the focus for quick flips)
            self.bump("dex_flip_buys")
        if nudge:
            sig.strength = round(max(0.0, min(1.0, sig.strength + max(-0.15, min(0.15, nudge)))), 3)
        advice_mult = max(0.4, min(1.5, advice_mult))
        exe = self.desk.bots.get("execution")
        if exe is not None and (left := exe.cooling(sig.symbol)) > 0:
            return self.reject(sig, f"{sig.symbol} is cooling off after a bad fill ({left / 60:.0f} min left)")
        coach = self.desk.bots.get("coach")
        quick = self.quick
        bar = coach.bar() if coach is not None else (s0.flip_min_strength if quick else limits.min_signal_strength)
        flipper = self.desk.bots.get("flipper")
        if flipper is not None:  # ⏱️ the hours that have made money get a slightly lower bar, losing hours a higher one
            bar = max(0.1, bar + flipper.bar_adj())
        if sig.strength < bar:
            return self.reject(sig, f"conviction {sig.strength:.2f} below threshold")
        if coach is not None and (left := coach.benched(sig.symbol)) > 0:
            return self.reject(sig, f"{sig.symbol} is benched by the Coach after repeated losses ({left / 60:.0f} min left)")
        if (why := self.desk.in_focus(sig.symbol)):
            return self.reject(sig, why)
        focus_max = self.desk.focus_status()["max_positions"]
        most = min(limits.max_open_positions, focus_max) if focus_max else limits.max_open_positions
        if len(self.portfolio.open_positions) >= most:
            return self.reject(sig, "max open positions reached" + (" for this balance (spread wider as it grows)" if focus_max else ""))
        meme = self.settings.is_meme(sig.symbol)
        if meme and sum(self.settings.is_meme(p.symbol) for p in self.portfolio.open_positions) \
                >= self.settings.max_meme_positions + ((s0.boost(self.desk.is_live) or {}).get("extra_memes", 0)):
            return self.reject(sig, "already holding the most meme coins allowed at once")
        if self.desk.is_live and self.desk.broker.supports(sig.symbol) is False:
            return self.reject(sig, f"{sig.symbol} can't be traded on your real-money {self.desk.broker.label} "
                                    "account (practice only)")
        since = now() - self.last_trade.get(sig.symbol, 0)
        if since < limits.symbol_cooldown_s:
            return self.reject(sig, f"symbol cooldown ({limits.symbol_cooldown_s - since:.0f}s left)")
        if self._rate_limited():
            return self.reject(sig, "order rate limit")

        snap = self.snapshots.get(sig.symbol)
        if snap is None:
            return self.reject(sig, "no indicator data")
        ind = snap.indicators
        atr = max(ind["atr"], price * 0.001)
        s = self.settings
        direction = sig.side.sign
        cost = s.round_trip_cost(sig.symbol, price)  # buy + sell fees and slippage, per unit
        if sig.source == "skimmer":  # a quick day-trade skim: small target, tight stop
            stop_dist = price * s.skim_stop_pct
            stop = price - stop_dist * direction
            skim = s.flip_skim_target_pct if quick else s.skim_target_pct
            tp = price * (1 + skim * direction) + cost * direction  # the target is after fees
            how = f"skim -{s.skim_stop_pct:.1%} / +{skim:.1%} after fees"
        elif s.stop_method == "percent":
            sl_pct, tp_pct = s.plan_pcts(sig.symbol, quick)
            if (learner := self.desk.bots.get("targets")) is not None:
                tp_pct = learner.target(sig.symbol, tp_pct)  # type: ignore[attr-defined]  # where this coin's moves end
            if s.patient(self.desk.is_live):  # a wider safety stop: losing trades get room to come back
                sl_pct = s.patient_meme_stop_pct if meme else s.patient_stop_pct
            if s.wide_stop:  # -7% (the wide stop, was -20%): small dips never stop the trade out (the size keeps the money at risk the same)
                sl_pct = max(sl_pct, s.wide_stop_pct)
            if sig.source == "pumpsniper":  # a pump: out quickly at its own nearer target (the same stop)
                tp_pct = s.pump_target_pct
            stop_dist = price * sl_pct
            stop = price - stop_dist * direction
            tp = price * (1 + tp_pct * direction) + cost * direction  # the target is after fees
            how = f"-{sl_pct:.0%} / +{tp_pct:.0%} after fees" + (" (quick flip)" if quick else "") \
                + (", patient" if s.patient(self.desk.is_live) else "")
        elif s.stop_method == "structure" and "swing_low" in ind:
            try:
                plan = plan_stop(
                    direction, price, atr, ind.get("swing_low"), ind.get("swing_high"),
                    ind.get("donchian_high"), ind.get("donchian_low"),
                    buffer_atr=s.stop_buffer_atr, min_stop_atr=s.min_stop_atr, max_stop_atr=s.max_stop_atr,
                    reward_risk=s.reward_risk, min_room_r=s.min_room_r,
                )
            except StopRejected as exc:
                self.bump("rejected_stop_placement")
                return self.reject(sig, str(exc))
            stop, tp, stop_dist, how = plan.stop, plan.target, plan.distance, plan.method
        else:
            stop_dist = s.stop_atr_mult * atr
            stop = price - stop_dist * direction
            tp = price + s.take_profit_atr_mult * atr * direction
            how = f"{s.stop_atr_mult:g}x ATR"

        # accuracy: only take trades whose target clearly pays for the fees
        edge = s.flip_min_edge if quick else s.min_edge_cost_multiple
        if quick and sig.source == "skimmer":
            edge = min(edge, 2.0)  # skims: a small target with their own tight stop (their reward still pays 2x the fees)
        reward = abs(tp - price)
        if reward < edge * cost:
            self.bump("rejected_fees")
            return self.reject(sig, f"target +{reward / price:.2%} doesn't clear fees "
                                    f"({cost / price:.2%} round trip x{edge:g})")

        equity = self.portfolio.equity
        risk_budget = equity * limits.risk_per_trade * (0.5 + 0.5 * sig.strength)
        strategist = self.desk.bots.get("strategist")
        if strategist and strategist.regime(sig.symbol) is Regime.VOLATILE:
            risk_budget *= 0.5
        if coach is not None:  # 🌱 grow: size up slowly after good results, down after bad ones
            risk_budget *= coach.risk_mult() * coach.symbol_mult(sig.symbol)
        if (reviewer := self.desk.bots.get("reviewer")) is not None:  # 📋 the trade finders that keep winning
            risk_budget *= reviewer.size_mult(sig.reason)  # type: ignore[attr-defined]
        if meme:  # meme coins swing hard: smaller bets
            b = s.boost(self.desk.is_live)
            risk_budget *= b["meme_risk_mult"] if b else s.meme_risk_mult
        if news is not None:  # scary world news: half-size new buys
            risk_budget *= news.size_mult(direction)
        if flipper is not None:  # ⏱️ bigger in proven good hours, smaller in proven bad ones
            risk_budget *= flipper.size_mult()
        risk_budget *= advice_mult  # what the advisors made of it
        risk_budget *= falling_mult  # (a buy in a falling market: smaller)
        risk_budget *= focus_mult  # (small / new coins first: a little bigger)
        if sig.source == "pumpsniper":
            risk_budget *= s.pump_size_mult  # (a pump snipe: smaller than a normal trade)
        if s.vol_target:  # equal risk across coins: calmer ones bigger, wilder ones smaller (research: vol targeting)
            vols = [x.indicators.get("volatility") or 0.0 for x in self.snapshots.values()]
            vols = sorted(v for v in vols if v > 0)
            mine = (self.snapshots.get(sig.symbol).indicators.get("volatility") or 0.0) if sig.symbol in self.snapshots else 0.0
            if vols and mine > 0:
                risk_budget *= max(0.5, min(1.5, vols[len(vols) // 2] / mine))
        if not guard["brake"] and s.reinvest_share > 0:  # 💰 reinvest: today's banked profit works too (house money)
            risk_budget += s.reinvest_share * max(0.0, self.realized_today())
        if guard["brake"]:  # a losing day: smaller trades until it turns
            risk_budget *= s.losing_day_size
        elif guard["locked"]:  # a good day slipping: keep trading (no cap on the upside), but smaller
            risk_budget *= s.day_lock_size
        size_dist = stop_dist
        if (sp := s.sizing_stop_pct(sig.symbol, self.desk.is_live)) is not None:
            size_dist = min(stop_dist, price * sp)  # full cash: sized for the patient cut, not the wide -7% stop
        qty = risk_budget / (size_dist + cost)  # a stopped-out trade also pays fees
        cap = limits.max_position_pct
        if meme and s.never_sell_at_loss:  # held without a cut, a meme coin can fall a long way: half the size
            cap *= s.never_sell_meme_size
        qty = min(qty, equity * cap / price)
        wanted = qty  # (before the room and cash limits: what a partial sale for it would have to pay for)
        room = equity * limits.max_gross_exposure - self.portfolio.gross_exposure
        qty = min(qty, max(room, 0) / price)
        budget_left = float("inf")
        if sig.side is Side.BUY and 0 < s.trend_split_falling_size < 1 and self._trend(sig.symbol) == "down":
            qty *= s.trend_split_falling_size  # a coin in a clear downtrend: a smaller bet
        if s.underwater_budget > 0 and sig.side is Side.BUY:  # the new trade starts below break-even too
            left = budget_left = equity * s.underwater_budget - self.desk.underwater_cost()
            if left <= 0:
                return self.reject(sig, f"underwater budget: {s.underwater_budget:.0%} of the balance is already in "
                                        "trades below break-even - no new buys until some recover")
            qty = min(qty, left / price)
        # buys need cash (no borrowing to make bets bigger)
        qty = min(qty, self.desk.free_cash() * 0.995 / price)
        # a buy at a good buy area on another coin (diversify): part of a trade in profit pays for it sooner
        short = 0.9 if s.diversify_rotation and buy_area(self.snapshots.get(sig.symbol)) else 0.5
        if (s.rotate_for_buys and sig.side is Side.BUY and sig.kind is SignalKind.ENTRY and qty < wanted * short
                and sig.symbol not in self._rotated and (donor := self._fund_from(sig.symbol, (wanted - qty) * price))):
            self._rotated.add(sig.symbol)
            self._rotation[sig.symbol] = (sig, now())
            return self.reject(sig, f"selling part of {donor} (in profit) to pay for it - bought once that sale fills")
        # portfolio heat: total money at risk to all stops stays under the cap
        heat_room = equity * limits.max_portfolio_heat - self.open_risk()
        if heat_room <= 0:
            return self.reject(sig, f"portfolio heat at limit ({limits.max_portfolio_heat:.0%} of equity at risk)")
        qty = min(qty, heat_room / (size_dist + cost))
        qty = self._round_qty(qty, price)
        if qty <= 0:
            return self.reject(sig, "insufficient capital / exposure room")

        # small accounts: a buy just under the venue's minimum order is rounded UP to it, if that stays inside
        # the position cap and risks at most 1.5x a normal trade (else it'd be unsellable dust: skipped below)
        checker = self.desk.live_broker
        if checker is not None and sig.side is Side.BUY:
            least = checker.min_qty(sig.symbol, price) * 1.02
            if 0 < qty < least and least * price <= min(equity * limits.max_position_pct, self.desk.free_cash() * 0.995) \
                    and least * (size_dist + cost) <= 1.5 * equity * limits.risk_per_trade and least * price <= budget_left:
                qty = least
        # never buy a coin that can't be sold again (venue lists it for selling, big enough, liquid enough)
        if checker is not None and s.is_crypto(sig.symbol) and sig.side is Side.BUY:
            why = checker.sell_check(sig.symbol, qty, price, strict=self.desk.is_live)
            if why:
                self.bump("rejected_unsellable")
                return self.reject(sig, f"won't buy what it can't sell: {why}")
        risk_amt = qty * (size_dist + cost)
        sig.reason += f" | stop {stop:,.6g} ({how}), target {tp:,.6g}, risk {risk_amt:,.2f}"
        self.strength_by_signal[sig.id] = sig.strength
        if len(self.strength_by_signal) > 5000:
            self.strength_by_signal.clear()
        self.approve(OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, False, stop, tp, price))

    def _past_floor(self, pos, price: float) -> bool:
        """The loss floor: never-sell holds a loser - unless it's this far below what was paid (then it's sold)."""
        floor = self.settings.max_loss_pct
        if floor <= 0 or not pos.avg_price:
            return False
        return (price / pos.avg_price - 1) * (1 if pos.qty > 0 else -1) <= -floor

    def _buy_back(self, sig: Signal, price: float) -> None:
        """Add coins back to a held position (bought back cheaper than they were sold): no new stop or target,
        the position keeps its own; paid from free cash, at least the venue's minimum order."""
        qty = self._round_qty(min(sig.qty or 0.0, self.desk.free_cash() * 0.995 / price), price)
        least = self.desk.broker.min_qty(sig.symbol, price)
        if qty <= 0 or qty < least:
            return self.reject(sig, "buy-back too small for the venue's minimum order")
        pos = self.portfolio.position(sig.symbol)
        if pos.stop_price is not None:  # the added coins sit above the same stop: they count toward portfolio heat
            limits = self.settings.limits(self.desk.is_live)
            room = self.portfolio.equity * limits.max_portfolio_heat - self.open_risk()
            per = max(price - pos.stop_price, 0.0) + self.settings.round_trip_cost(sig.symbol, price)
            if per > 0 and qty * per > room:  # (a partial buy-back would end with fewer coins: wait instead)
                return self.reject(sig, "buy-back waits: portfolio heat at its limit")
        self.approve(OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, False, None, None, price))

    def _dip_add(self, sig: Signal, pos, price: float) -> None:
        """Buy more of a coin held at a loss, lower down: within the normal biggest position, leaving cash free,
        and only if no advisor objects (a flash crash, a falling market, a dead market)."""
        s = self.settings
        snap = self.snapshots.get(sig.symbol)
        for adv in self.desk.advisors:
            if adv.enabled:
                veto, _, _ = adv.advise(sig, snap)
                if veto:
                    self.bump("rejected_by_advisors")
                    return self.reject(sig, f"{adv.title}: {veto}")
        equity = self.portfolio.equity
        cap = s.limits(self.desk.is_live).max_position_pct
        if s.is_meme(sig.symbol) and s.never_sell_at_loss:
            cap *= s.never_sell_meme_size
        room = equity * cap - abs(pos.qty) * price  # the trade never grows past the normal biggest position
        if s.underwater_budget > 0:  # averaging down adds to what's below break-even
            room = min(room, equity * s.underwater_budget - self.desk.underwater_cost())
        spare = self.desk.free_cash() - s.dip_keep_cash * equity  # and a share of the balance stays in cash
        qty = self._round_qty(min(sig.qty or 0.0, max(room, 0.0) / price, max(spare, 0.0) * 0.995 / price), price)
        least = self.desk.broker.min_qty(sig.symbol, price)
        if qty <= 0 or qty < least:
            return self.reject(sig, "dip buy skipped: no room (biggest position reached, or cash kept free)")
        self.approve(OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, False, None, None, price))

    def realized_today(self) -> float:
        """Profit banked today (closed trades and slices, after fees) in the current book."""
        start = calendar.timegm(time.strptime(self.desk._day, "%Y-%m-%d")) if self.desk._day else 0.0  # UTC midnight
        total = 0.0
        for t in reversed(self.portfolio.closed_trades):
            if t.ts < start:
                break
            total += t.pnl
        return total

    def open_risk(self) -> float:
        """Money that would be lost if every open position hit its stop (locked profits count 0)."""
        total = 0.0
        for p in self.portfolio.open_positions:
            if p.stop_price is None:
                total += abs(p.market_value)  # unprotected position: whole value at risk
                continue
            risk = max(0.0, (p.avg_price - p.stop_price) * p.qty)
            if (sp := self.settings.sizing_stop_pct(p.symbol, self.desk.is_live)) is not None:
                risk = min(risk, abs(p.qty) * p.avg_price * sp)  # (counted the way it was sized)
            total += risk
        return total

    # ------------------------------------------------------------------
    ROTATION_WAIT_S = 120.0

    def _fund_from(self, symbol: str, usd: float) -> str | None:
        """Sell part of a trade in profit after both fees - the one whose signal is weakest now - to pay ``usd`` of a
        new buy. Returns the coin sold from, or None when no trade can pay for it without a loss."""
        s = self.settings
        quant = self.desk.bots.get("quant")
        scores = getattr(quant, "scores", {}) if quant is not None else {}
        best = None
        for pos in self.portfolio.open_positions:
            if pos.symbol == symbol or pos.qty <= 0 or s.is_stack_only(pos.symbol):
                continue
            px = self.desk.prices.get(pos.symbol) or pos.last_price
            if not px or not s.beats_fees(pos, px):
                continue  # (only a trade whose sale keeps a profit after fees: never sold at a loss)
            value = -pos.qty * px if s.diversify_rotation else 0.0  # (diversify: the biggest trade pays first)
            rank = (value, scores.get(pos.symbol, {}).get("ensemble", 0.0), -s.net_if_sold(pos, px))
            if best is None or rank < best[0]:
                best = (rank, pos, px)
        if best is None:
            return None
        _, pos, px = best
        frac = min(s.rotate_max_share, usd * 1.01 / (pos.qty * px))
        if frac <= 0:
            return None
        self.publish("signal.proposed", Signal(pos.symbol, Side.SELL, 1.0, f"partial sale to pay for a {symbol} buy",
                                               self.name, SignalKind.EXIT, fraction=frac))
        self.bump("rotations")
        self.act(f"selling {frac:.0%} of {pos.symbol} (in profit) to pay for a {symbol} buy")
        return pos.symbol

    async def on_cycle(self) -> None:
        for sym, (sig, at) in list(self._rotation.items()):  # buys waiting for a partial sale's cash
            if now() - at > self.ROTATION_WAIT_S:
                del self._rotation[sym]
                self._rotated.discard(sym)
            elif self.desk.free_cash() > 1.0:
                del self._rotation[sym]
                again = Signal(sig.symbol, sig.side, sig.strength, f"{sig.reason}; paid for by a partial sale", sig.source,
                               votes=getattr(sig, "votes", None) or {})  # (a fresh idea: each id is decided once)
                self.evaluate(again)  # (sized to the cash now free; not funded twice)
                self._rotated.discard(sym)
        p = self.portfolio
        limits = self.settings.limits(self.desk.is_live)
        self._track_day_peak()
        self.metrics.update(
            {
                "drawdown": round(p.drawdown, 4),
                "daily_pnl_pct": round(p.daily_pnl_pct, 4),
                "gross_exposure": round(p.gross_exposure / p.equity if p.equity > 0 else 0, 4),
                "portfolio_heat": round(self.open_risk() / p.equity if p.equity > 0 else 0, 4),
            }
        )
        if self.desk.halted:
            return
        s = self.settings
        daily_lim, dd_lim = limits.max_daily_loss_pct, limits.max_drawdown_pct
        if self.desk.is_live:  # real money: the stricter of the two
            live_daily, live_dd, _ = s.live_limits()
            daily_lim, dd_lim = min(daily_lim, live_daily), min(dd_lim, live_dd)
        reason, kind = None, "manual"
        principal = p.starting_cash + p.net_flows  # what you put in
        base = min(principal, self.desk.floor_ack.get(self.desk.mode, principal))
        if self.desk.is_live and base > 0 and p.equity < base * (1 - s.live_limits()[2]):
            reason, kind = (f"your real money fell {1 - p.equity / base:.1%} below what you put in - "
                            "everything stopped to protect what's left"), "floor"
        elif p.daily_pnl_pct <= -daily_lim:
            reason, kind = f"daily loss {p.daily_pnl_pct:.2%} breached limit", "daily"
        elif p.drawdown >= dd_lim:
            reason, kind = f"drawdown {p.drawdown:.2%} breached limit", "drawdown"
        if reason:
            if self.settings.never_sell_at_loss and p.open_positions:
                reason += " - new trades stopped; open trades are held, never sold at a loss"
            self.act(f"KILL SWITCH: {reason}", "critical")
            self.publish("alert", Alert("critical", f"Kill switch tripped: {reason}", self.name))
            # never sell at a loss: the limit stops new trades, but open ones are held rather than sold at the low
            self.desk.halt(reason, flatten=not self.settings.never_sell_at_loss, kind=kind)
