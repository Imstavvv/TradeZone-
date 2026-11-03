# TradeZone

A multi-bot trading platform. Eleven specialised bots, including an AI analyst powered by xAI's Grok and a Coach that learns from every trade, each with its own role, work as a desk. They talk over an async event bus, and you supervise them from a real-time web dashboard with pause, flatten and kill-switch controls.

It trades **mainly crypto**, including quick flips on trusted meme coins. It runs in **paper mode by default**, trading **live market prices** with simulated fills and no money at risk. The bots learn on the same data they'll later trade live. Live trading through Alpaca is supported, but you have to opt in explicitly (see [Live trading](#live-trading)).

## The bot team

| Bot | Role | What it does |
| --- | --- | --- |
| **Scout** | Market data and signal prep | Ingests every tick, rejects bad data, detects shock moves and stale feeds, and computes EMA, RSI, volatility, ATR and Donchian channels |
| **Strategist** | Market regime analysis | Classifies each symbol as *trending*, *ranging* or *volatile* (Kaufman efficiency ratio and volatility ratio, with hysteresis) |
| **Quant** | Strategy and signal generation | Runs momentum, mean-reversion and breakout models, weights them by the Strategist's regime call, and only proposes a buy when the team reaches consensus (see below) |
| **Grok** | AI analyst (xAI Grok) | Sends every proposed new position, with indicators, regime, recent prices and portfolio context, to Grok for an independent second opinion. In `veto` mode, trades Grok doesn't approve are never placed |
| **Risk Manager** | Risk control and position sizing | Gates every signal: kill switch, pause, exposure, concentration, max positions, cooldowns, order-rate limit. Sizes positions by volatility (risk to stop) and halves size in volatile regimes. Trips the kill switch on daily-loss or drawdown limits |
| **Executive** ★ | Head trader: final decisions | Takes the final real-time decision on every trade. Nothing reaches the broker without its authorisation. Checks team health (live prices, safety bots on duty, books balanced), re-checks the live price so it never chases a move that already happened, caps correlated crypto bets, and switches to a defensive stance (half size, higher conviction) after a losing streak or deep drawdown. Exits are always authorised instantly. Decision time is measured and shown, typically well under 1 ms. |
| **Executor** | Order execution and routing | Runs pre-trade checks, sends orders to the broker, blocks duplicates, enforces reduce-only exits, verifies every fill, and cancels stale orders |
| **Portfolio Manager** | Positions, P&L and exits | Books fills (average-cost accounting), marks positions to market, manages stop-loss, take-profit, trailing stops and profit protection, and reconciles against the broker |
| **Sentinel** | Audit, compliance and performance | Journals every decision to SQLite, keeps an independent audit ledger that is checked against the books every cycle, computes Sharpe, drawdown, win rate and profit factor, and raises alerts when any bot errors |
| **Skimmer** 🏄 | Profit taking and day-trade skims | Takes profits of every size (see [The Skimmer](#the-skimmer-profits-big-and-small)) |
| **Coach** 🎓 | Learning and team rules | Learns from every closed trade which strategies, coins and market moods work, and nudges the team within safe limits (see [Team rules and learning](#team-rules-and-learning)) |

### How a trade flows

```
market.tick ─► Scout ─► market.snapshot ─► Strategist ─► (regime)
                                     └──► Quant ─► signal.proposed ─► Grok (veto) ─► Risk Manager
                                                                         │ approve / reject
                                                     order.approved ◄────┘
                                                        │
                                              Executive (final real-time call: authorise / hold)
                                                        │ order.authorized
                                                    Executor ─► broker ─► order.filled
                                                                              │
                         Portfolio Manager ◄──────────────────────────────────┘
                         (stops / targets) ─► exit signal ─► Risk Manager (reduce-only)
                         Sentinel listens to everything ─► journal + analytics + alerts
```

Safety properties:

* Every order goes through the Risk Manager. If you disable the Risk Manager, no new orders are placed at all.
* Exits are reduce-only and are always allowed, even while paused or halted, because they only lower risk.
* A failing bot handler is isolated by the bus. Its error is counted, and the Sentinel raises an alert.
* Safety-critical bots can't be disabled from the API while positions are open.

## A fun, friendly interface

**🎯 Today's hunt** (top of the Home screen) shows what the bots did today: chances checked, how many the team liked, trades opened, and wins out of trades closed. It also lists, in plain words, the most common reasons they skipped, so you can tell the bots are working even when they're not buying. If something is stopping them altogether (no live prices, paused, or emergency stop), the card says so.

**Anime power-up theme:** an original look inspired by classic 90s battle anime. It has a sunset-orange and sky-blue palette, comic-panel cards with ink outlines, and speed lines behind your balance. A **⚡ power level** readout shows your balance, with a meter for today's move and a rank that grows with your total return (Rookie → Fighter → Elite → Champion → Legend). Your win streak shows as glowing energy orbs. A golden aura pulses around the balance card on an up day, bots glow when they act, and winning trades end with a **POWER UP!** burst. It's all CSS, with no outside fonts or images, so the security policy stays strict. Animations switch off if your phone has "Reduce Motion" on.

TradeZone opens in **Simple view**: a colourful home screen in plain English. The **Pro** switch in the header shows the full trading dashboard.

* **Hero card:** your balance, which counts up or down as it changes, and today's result with a mood (😎 great day, 🙂 up, 😌 steady, 🥶 rough day). A live price ticker scrolls across the top. Practice money is violet-pink; real money switches to a fiery red-orange so the two can't be confused.
* **Badges:** 🔥 win streaks, 🔒 profits locked in, and ⚡ how fast the Executive decides.
* **Your bot crew:** every bot has a mascot (🔭 Scout, 🧭 Strategist, 🧮 Quant, 🤖 Grok, 🛡️ Risk Manager, 👑 Executive, ⚡ Executor, 💼 Portfolio Manager, 🏄 Skimmer, 🦉 Sentinel, 🎓 Coach). Each one bounces while it works and says what it's doing in a speech bubble.
* **Celebrations:** a confetti burst and a 🎉 message when a trade closes in profit. Losses get a calm note, because small losses are part of the plan.
* **Big, friendly buttons** (Pause, Close all, Emergency stop, Use real money), in-app confirmation sheets instead of browser pop-ups, and a small vibration on Android.
* **Texture:** drifting colour glows, film grain and frosted-glass cards, in the rounded system font on Apple devices.
* With **Reduce Motion** turned on in your phone's settings, every animation and the confetti switch off.

## Watching the bots work, live

The dashboard streams everything the team does the moment it happens, over a WebSocket:

* **Bot floor:** the 11 bots in the order a trade flows through them (Scout → Strategist → Quant → Grok → Risk Manager → **Executive** → Executor → Portfolio Manager → Skimmer → Sentinel → Coach). The header shows the Executive's stance and decision speed. Each tile shows what the bot is doing right now, how busy it is (actions/min, total), when it last acted, and **lights up in its colour** whenever it acts. Tap a tile (or a bot card) for that bot's full log of its last 200 actions, which keeps updating while open.
* **Live activity:** every bot "thought" (e.g. *no consensus to buy AAPL: momentum against*) and every team event (signals with their votes, Grok verdicts, Risk approvals and rejections with the reason, orders, fills, stop moves, alerts). Filter by Trades, Signals, Risk, Alerts, Bot thoughts or any single bot.
* **Trade log:** every fill, stored permanently in the journal and shown live as it happens. Each entry has paper/live, symbol, open/close/partial/add, side, quantity, price, fee, realized P&L and **why**: the full reason, including consensus votes, stop placement and risk, or "stop hit at…". Filter by mode and symbol, load more, and **Export CSV** for spreadsheets or taxes. Totals (fills, realized P&L, fees, win rate) follow the filter.

## Autonomous operation

Once started, the desk runs on its own around the clock:

| What happens | How |
| --- | --- |
| **Real-time prices** | Coinbase's WebSocket stream delivers every trade as it happens (⚡ in the header). If the stream drops, it reconnects with back-off and polls the REST API in the meantime, a few symbols per cycle to stay under Coinbase's rate limit. |
| **Instant start** | 5 hours of 1-minute candles are loaded at startup, so the bots trade within seconds instead of after a warm-up. Historical bars warm the indicators but are never traded on. |
| **10 markets scanned** | BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK, LTC and DOT against USD, each on 1-minute bars. Exits react to every streamed trade. |
| **Restarts don't lose positions** | Books (cash, positions, stops, targets, profit locks), mode and kill-switch state are saved to the journal database after every fill and control action. On restart everything is restored. If the desk was live, it resumes live, but **only** if the broker's positions match the saved book. Otherwise it stays on paper and raises a critical alert. |
| **Daily loss limit re-arms itself** | A kill switch tripped by the daily-loss limit resets at the next UTC day (`TZ_AUTO_REARM_DAILY`). |
| **Waits for you** | The drawdown kill switch, execution-integrity pauses and book mismatches are deliberate stop-and-look moments, so they don't auto-resume. Going live is always your decision (Go LIVE button). |

### Costs are realistic, and trades must beat them

Paper fills charge what live trading costs: **0.25% per side on crypto** (Alpaca's base taker fee) and 0% on US stocks, plus 0.03% slippage. A trade is skipped unless its profit target is at least **3× the round-trip cost** (`TZ_MIN_EDGE_COST_MULTIPLE`), and position sizing counts fees as part of the risk. That's the main defence against the most common way fast bots lose money: many small trades that each pay the fee and add up to a loss.

With realistic fees, the bundled strategies came out roughly break-even in offline simulation (−0.7% to +0.5% across 4 seeds, 26–34 trades each, max drawdown ≤1.4%, no winner closing as a loss). Synthetic data says little about real markets. Paper-trade on live prices for a good while before going live.

## Market data: paper trades live prices

By default (`TZ_FEED=auto`), paper trading uses **real market data**. If Alpaca keys are set it uses Alpaca (stocks and crypto, polled). Otherwise it uses Coinbase's public real-time stream (crypto, no account needed).

Indicators and signals run on **1-minute bars** (`TZ_BAR_SECONDS`), pre-loaded from history on Coinbase. Stops, targets and profit locks react to every trade. On Alpaca there's no history pre-load yet, so expect a 30-bar warm-up (shown in a banner).

`TZ_FEED=sim` (synthetic prices) remains available for offline demos, the tests and `python -m tradezone sim`.

## Team rules and learning

Every bot works by four team rules, and each bot has a **mission** saying which rule it serves (shown on its card):

| Rule | What it means in practice |
|---|---|
| 💰 **Make money** | Only take trades whose target beats the risk and pays for the fees (Risk Manager, Executive, Executor). |
| 🎯 **Take trades** | Don't sit idle: if hours pass with no trade while results are healthy, the Coach lowers the conviction bar a little (by at most 0.05). |
| 🌱 **Grow** | Sizes already scale with the balance, so gains compound. After 15+ trades with a profit factor of 1.2 or more, risk per trade rises to 1.1x (1.25x at 1.5 or more). After losses it drops to 0.8x or 0.6x. |
| 🧠 **Learn the markets** | The Coach credits each finished trade to the strategies that suggested it, the coin and the market mood. |

What the Coach changes, and the guard rails:

* **Strategy trust (0.5x to 1.5x):** a strategy that keeps winning gets more say in the Quant's vote. Records are shrunk toward 50/50 until there is enough evidence, so a lucky streak can't take over.
* **Coins:** after 5+ trades with a poor record and a net loss, a coin sits out for `TZ_COACH_BENCH_MINUTES` (120 by default). Coins with a good record get slightly bigger positions (up to 1.2x), poor ones smaller (down to 0.8x).
* **Hard limits never move:** stops, position and exposure caps, portfolio heat, daily loss and drawdown kill switches, and the fee filter all apply exactly as before.
* What the team has learned survives restarts, and it is shown in plain words in the "What the team has learned" card.
* `TZ_COACH_LEARNING=false` turns learning off (the rules still apply). `TZ_COACH_IDLE_HOURS` (6 by default) sets how long the team waits before looking for more chances.

Learning can't promise profits. In four 3,000-step simulations on synthetic prices (seeds 1 to 4), ending equity with learning on was about $760 higher in total than with it off. Drawdown was the same or lower in every run. One seed was unchanged and another still lost money. Treat this as a check that the feature works safely, not as a forecast.

## Buys need team consensus

A buy only happens when the team agrees. Each voter says yes, no or abstain:

| Voter | Votes yes when… | Votes no when… |
| --- | --- | --- |
| Momentum model | fast trend is up | fast trend is down |
| Mean-reversion model | price is oversold | price is overbought |
| Breakout model | price breaks the 20-bar high | price breaks the 20-bar low |
| Strategist | trending up, or ranging and oversold | volatility storm, or trending down |

**Rule:** no voter that matters in the current kind of market may say no, and at least `TZ_CONSENSUS_MIN_YES` (default 2) must say yes. A model that barely counts in that market abstains instead of vetoing. In a trend, the dip-buying model doesn't block trend trades, and in a sideways market the trend models don't block dip-buys. On meme coins, where "volatile" is normal, the Strategist abstains instead of refusing. Before this rule, the out-of-place objections blocked about 9 in 10 otherwise good setups. After that, **Grok** must approve (if connected) and the **Risk Manager** must approve. The Quant re-checks on every bar while its view holds, so a buy goes ahead as soon as the last objection clears. Each signal's votes appear in Team activity, for example `[momentum:✓ mean-rev:– breakout:✓ strategist:✓]`. Selling to close a position needs no vote.

## What the bots trade: crypto, quick meme-coin flips, and runners

By default the bots trade **crypto only**: majors (BTC, ETH, SOL, XRP, AVAX, LINK) plus **trusted meme coins** (DOGE, SHIB, PEPE, BONK, WIF, FLOKI). These are the large, long-listed ones on major US exchanges; brand-new tokens are never traded. Change the list with `TZ_SYMBOLS` and `TZ_MEME_COINS`. Coins your exchange doesn't list are skipped at start-up with a note.

* **Meme coins: −8% stop-loss, +15% take-profit** (`TZ_STOP_LOSS_PCT`, `TZ_TAKE_PROFIT_PCT`). Meme coins can move that far in hours.
* **Bigger coins (BTC, ETH, SOL, …): −3% / +6%** (`TZ_MAJOR_STOP_LOSS_PCT`, `TZ_MAJOR_TAKE_PROFIT_PCT`). They move about 0.1% a minute, so ±8–15% would take days, and the bots would sit on a few positions doing nothing. Set these to 0.08 / 0.15 if you want one plan for everything.
* **Quick flips free up stuck trades:** a trade that hasn't reached its target after 6 hours (meme coins) or 24 hours (others), but is in profit after fees, is closed so the slot goes to a fresh chance. Losing trades are never closed this way; they keep their stop.
* **Sniping meme coins:** when a trusted meme coin breaks more than 1% above its recent high, the Quant acts on that tick instead of waiting for the minute to close. Its momentum must agree, and the rest of the team (Risk Manager, Executive, and Grok if connected) still has to say yes. A breakout against a falling trend is skipped. One shot per breakout.
* **Runners:** at the target the bots bank half the position. The stop on the other half moves **into profit** (at least half the target: +7.5% for meme coins, +3% for others), the ceiling is removed, and the stop then trails under the highest price by the stop distance. A model "sell" signal doesn't cut a runner short; only its trailing stop closes it.
* **Profit protection** still applies before the target: once a trade is about +4% up, its stop moves above break-even, so a trade that went green doesn't end red.
* **Safer sizing for meme coins:** they risk half of what a normal trade risks (`TZ_MEME_RISK_MULT=0.5`), and at most 3 are held at once (`TZ_MAX_MEME_POSITIONS`).
* **Real money:** Alpaca can trade DOGE, SHIB and PEPE (plus the majors) but not BONK, WIF or FLOKI. Those stay practice-only, and the Risk Manager says so instead of sending an order that would fail.

Meme coins can move 20–50% in minutes. The −8% stop limits each loss, but a price that jumps straight through the stop fills at the next price. **No setting can guarantee profits.**

## The Skimmer: profits big and small

The 🏄 **Skimmer** does two jobs:

1. **Quick day-trade skims.** On the bigger, calmer coins (not meme coins, which get snipes), it buys a short **dip inside an uptrend**: the fast trend is up, the price has dipped below its fast average, and RSI has cooled to 30–45. The target is small (**+2.5%**), the stop tight (**−1.2%**), and each skim has a **2-hour** time limit. When time is up, a skim in profit after fees is sold. A skim in a loss is never dumped by the clock; it keeps its stop. Skims only happen when the market is calm enough for that tight stop, and when a +2.5% move is realistic within 2 hours. They still need the team's vote (trend and dip both agree, nobody objects) and go through the Risk Manager and the Executive like every trade. At most 2 skims are open at once, with a 15-minute pause before skimming the same coin again.
2. **Taking profit on every open trade:**
   * **Small:** once a trade is **halfway to its target**, 25% is banked.
   * **Large:** if the price **spikes** within the last 5 bars (+2.5% on bigger coins, +6% on meme coins), **half** is sold into the jump. Spikes often fall back.
   * Each happens once per trade (a spike sell at most every 10 minutes), only ever at a **profit after fees**, and always as a reduce-only sell, so it can never add risk.

Stops, the break-even lock, targets and runners still belong to the Portfolio Manager; the Skimmer adds earlier profit-taking on top. Settings: `TZ_SKIMMER`, `TZ_SKIM_TARGET_PCT`, `TZ_SKIM_STOP_PCT`, `TZ_SKIM_MAX_MINUTES`, `TZ_MAX_SKIMS`, `TZ_BANK_AT_SHARE`, `TZ_BANK_FRACTION`, `TZ_SPIKE_PCT`, `TZ_MEME_SPIKE_PCT`, `TZ_SPIKE_FRACTION`.

Taking profits early is a trade-off: it raises the share of winning trades and smooths the ride, but it can give up part of a big run. Fees (about 0.6% round trip on crypto) are why skims aim for +2.5% rather than tiny moves.

## Stop-loss placement and risk management

The default is the fixed **−8% / +15%** plan above (`TZ_STOP_METHOD=percent`). Two other placements are available.

With `TZ_STOP_METHOD=structure`, stops go where most traders put them, **just beyond the most recent swing point**:

* **Long:** below the lowest low of the last 10 bars (the last dip buyers defended), minus a 1×ATR buffer so ordinary noise doesn't tag it. Shorts mirror this above the swing high.
* **Off round numbers:** if the stop lands on or near a round number (100, 105, 65,000…), where stops cluster and get hunted, it moves just beyond it.
* **Never too tight:** at least 1.5×ATR from entry.
* **Never too wide:** if the swing stop is more than 4×ATR away, the trade is **skipped** rather than taken with oversized risk.
* **Target:** 2× the stop distance (2:1 reward-to-risk). The trade is skipped if resistance is less than 1R away, because it would have no room to work.
* **Fallback:** if there's no swing level yet, the stop is 2×ATR. `TZ_STOP_METHOD=atr` switches everything to plain ATR stops.

Risk management layered on top:

| Rule | Default |
| --- | --- |
| Risk per trade: position sized so hitting the stop loses at most this | 1% of equity (half that in volatile regimes and for meme coins) |
| Portfolio heat: total loss if *every* open stop is hit | 5% of equity |
| Max position size / total exposure / open positions | 20% / 100% / 5 |
| Daily loss / drawdown kill switch (flattens everything) | −5% / −15% |
| Profit protection after +0.5R | stop to break-even + costs, then locks 50% of peak profit |

Every approved trade's reason in Team activity and Orders shows its stop, how it was placed, the target and the money at risk. For example: `stop 98.8 (below swing low 99.2 - 1 ATR buffer), target 102.4, risk 412.50`.

## Profit protection

**No system can guarantee every trade ends in profit.** A price can fall right after a buy, before the trade is ever green. Removing stop-losses wouldn't change that; it would only turn small losses into large ones. What TradeZone enforces is the next best thing: **once a trade has been in profit, it's not allowed to turn into a loss.**

1. **Break-even lock:** when open profit reaches 0.5R (half the distance from entry to the initial stop), the stop moves above break-even plus round-trip fees, slippage and a cushion sized to the symbol's typical tick move. From then on it locks in 50% of the best open profit seen. These positions show a 🔒 in the dashboard.
2. **Partial take-profit:** with the default −8% / +15% plan, half is sold at +15% and the rest becomes a **runner** (see above). With `structure` or `atr` stops, half is sold at +1R and the rest rides with the locked stop toward the target.
3. **Strategy exits only in profit:** a bearish model signal closes a position only if that nets a profit. Losing positions are left to the stop-loss, which still cuts them. Your Flatten button and the kill switch always work regardless.

The **Winners lost** tile counts any winner that still closed at a loss. That can only happen if the price jumps straight through the stop between two ticks, and each case raises an alert. In offline simulation (4 seeds × 3,000 steps, realistic fees) the count was 0 in every run, with win rates of 50–72%. That's synthetic data, not a promise about real markets.

## Grok (xAI) connection

**Easiest:** tap the **Grok** button (or the GROK badge) in the dashboard, paste your xAI API key, choose the mode, and tap **Save & connect** (it asks for your password or Face ID). The key is stored on your server only, shown afterwards as `…abcd`, and never sent back to the browser. It survives restarts, and it overrides `TZ_GROK_API_KEY` if both are set.

Or set it on Render as an environment variable:

```bash
TZ_GROK_API_KEY=xai-...        # XAI_API_KEY or GROK_API_KEY also work
TZ_GROK_MODEL=grok-4           # optional: if the account lacks it, the best available Grok model is picked
TZ_GROK_MODE=veto              # veto (default when a key is set) | advisory | off
TZ_GROK_FAIL_OPEN=false        # if Grok is unreachable, hold new entries (the safe default)
```

* **veto:** new positions need Grok's approval. Its confidence is blended into the position size, and signals that go stale while waiting are dropped.
* **advisory:** Grok's opinion is logged and shown, but it never blocks.
* **Exits never wait for Grok.** Stops, targets, flattening and the kill switch always go straight through.

**If it won't connect**, the Grok screen and a dashboard banner say why, in plain English:

| Message | Fix |
| --- | --- |
| *xAI rejected the API key* | Copy a fresh key from console.x.ai → API Keys. They start with `xai-`. |
| *no credits / spending limit* | Add credits or raise the spending limit at console.x.ai. |
| *Model … isn't available* | Usually fixed automatically by switching to the best available Grok model. Otherwise clear the Model field. |
| *rate limit* / *timeout* / *can't reach api.x.ai* | Temporary. The connection is re-checked every minute while down. |

While Grok is down in veto mode, **new trades are on hold**: the team requires Grok's approval and it can't be had. Fix the key, or switch Grok to *Advisory* or *Off* to trade without it.

## Accuracy: what is and isn't guaranteed

**No system can make every trade profitable.** Markets aren't predictable enough, and anything promising "100% winning trades" is misleading. What TradeZone enforces is **100% execution accuracy**: every trade happens exactly as intended, and the desk stops itself the moment anything doesn't match.

| Check | Where | On failure |
| --- | --- | --- |
| Symbol on the approved list, finite positive quantity, valid reference price | Executor (pre-trade) | Order blocked |
| Fat-finger guard: entry notional within the position cap | Executor (pre-trade) | Order blocked |
| Idempotency: no replayed order requests or duplicate signal ids | Executor, Risk Manager | Order blocked |
| Every fill matches the order's symbol and side, never overfills, and stays within `TZ_MAX_SLIPPAGE_BPS` of the reference price | Executor (post-fill) | Trading paused, critical alert |
| Independent audit ledger (cash and every position) rebuilt from fills and funding events, compared to the books **every cycle** | Sentinel | Trading paused, critical alert |
| Ledger positions match the broker's positions | Portfolio Manager (every 30 cycles) | Trading paused, critical alert |

The dashboard shows **Exec accuracy** (verified fills ÷ all fills) and whether the books are balanced. When something trips, it shows a banner with the reason.

To make the desk more selective, rather than "more accurate", raise `TZ_MIN_SIGNAL_STRENGTH` or run Grok in `veto` mode. You'll get fewer trades, with stronger evidence behind each one.

## Wallets, deposits and withdrawals

Link wallets in the **Wallets & funding** panel or via the API. Supported chains: BTC, ETH, SOL, XRP, XLM, ATOM, HBAR and EOS.

* **Address validation:** every address is format-checked for its chain.
* **Withdrawal tags and memos:** XRP destination tags, and XLM, ATOM, HBAR and EOS memos, are validated and attached to every withdrawal to that wallet. If you leave the tag empty on a tag chain, you must explicitly confirm the destination doesn't need one, because a missing tag is the classic way to lose exchange deposits.
* **Deposit tags:** every linked wallet gets a unique numeric deposit tag. Deposits that carry it are credited to that wallet. On chains without tags, deposits are matched by sender address. Unmatched deposits are **held** for review and credited only when you attribute them (`POST /api/deposits/{id}/credit`). A transaction hash can't be credited twice.
* **Withdrawal safety:** withdrawals go only to linked wallets, only after a cooldown on newly linked wallets (`TZ_WITHDRAWAL_COOLDOWN_H`, default 24h), within a rolling 24h limit (`TZ_MAX_DAILY_WITHDRAWAL`) and never above free cash.
* **Funding isn't P&L:** deposits and withdrawals move the P&L baselines, so returns and the kill switch aren't distorted.
* **Deposit addresses:** set the desk's own receiving addresses with `TZ_DEPOSIT_ADDRESSES=XRP:r...,ETH:0x...`.

> **Paper vs live.** In paper mode, deposits and withdrawals move simulated cash. The "Record deposit" form stands in for an on-chain watcher, which isn't included. In live mode, your funds sit at your broker, so withdrawals are recorded as `manual_required` and aren't debited until you complete them at the broker. TradeZone never moves real crypto on its own.

## Security

The site controls real money, so it's locked down in layers. Each is tested in `tests/test_security.py`, including simulated attacks from a malicious site.

| Threat | Protection |
| --- | --- |
| Someone finds your site | Nothing works without signing in with your password (or Face ID). Until a password is chosen, setting up needs the one-time SETUP code that is only printed in the server logs. An optional `TZ_API_TOKEN` must be at least 16 characters on a public address, or the server refuses to start. `/api/health` reveals nothing, and the API docs (`/docs`, `/openapi.json`) are switched off. |
| Guessing the token | After 5 wrong tokens in 15 minutes (`TZ_LOGIN_FAILURES_BEFORE_BAN`) the IP is blocked for 30 minutes (`TZ_BAN_MINUTES`), doubling for each repeat offence up to 7 days. Bans survive restarts. Devices already signed in keep working, so someone guessing on your Wi-Fi can't lock you out. |
| Stolen token from the page or logs | The token is exchanged once for an **HttpOnly, Secure, SameSite=Strict session cookie**. Page scripts can't read it, and it never appears in a URL or the server's request logs. Sessions expire after 30 days unused. **Sign out all devices** cuts off any stolen session instantly. |
| Stolen session moving money | Going live, adding a withdrawal wallet and withdrawing need the **token typed again** (step-up). New wallets also wait 24 hours before they can receive withdrawals. |
| Malicious websites acting as you (CSRF, WebSocket hijacking) | State-changing requests and live connections from another site are refused (Origin check), and the cookie is never sent cross-site. |
| Clickjacking (your dashboard hidden inside another page) | `frame-ancestors 'none'` and `X-Frame-Options: DENY`. |
| Injected scripts (XSS) | A strict Content-Security-Policy with a per-request nonce: only TradeZone's own script runs. All displayed data is escaped. |
| Flooding / denial of service | 600 requests per minute per IP (`TZ_RATE_LIMIT_PER_MIN`), a 64 KB request size limit, at most 6 live connections per IP (60 total), capped concurrency and short keep-alives. |
| Unknown networks | Optional firewall allowlist: `TZ_ALLOWED_IPS=203.0.113.7,198.51.100.0/24` lets only those addresses in. The platform health check still works. |
| Spoofed IP addresses | Forwarded-for headers are only trusted from the hosting proxy (`TZ_TRUSTED_PROXIES`, set automatically on Render). |
| Not knowing what happened | The **Security** panel shows signed-in devices, blocked IPs (with Unban), and a log of every sign-in, failure, ban and sensitive action, with IP and device. It's also stored in the journal. |

Also sent: HSTS, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, cross-origin isolation headers, `Cache-Control: no-store` on the API, and no `Server` header. If you think someone else got in, use **Settings → Log out everywhere** and change your password. If you use an access token for scripts and suspect it leaked, change `TZ_API_TOKEN` on Render and redeploy.

### Logging in: a password your phone remembers (or Face ID)

Every visit starts on the **front page**: the TradeZone logo, your bot mascots, and **one** way to log in: a big Face ID button if you use Face ID, otherwise a password box (with a small link to switch).

* **First visit ever:** the front page asks for the one-time **setup code**. Find it in Render → your service → **Logs**, on the line `TradeZone SETUP code`. Then you pick **one** way to log in: **Face ID / fingerprint** (nothing to remember) or a **password** (at least 6 letters or numbers; spaces and emoji are fine). You can add the other later in Settings. Nobody can set up your site without that code, and it's only used up once your login is saved, so a cancelled Face ID prompt or a too-weak password can simply be retried.
* **Let your phone save it.** The login box is a normal password form, so iPhone (iCloud Keychain), Android (Google Password Manager) and desktop browsers offer to save it and fill it in with Face ID or your fingerprint. Logging in becomes one tap.
* **You stay logged in.** A device you've logged in on stays logged in for 30 days from your last visit, including the home-screen app. It doesn't ask for the password every time you open it. **Settings → Lock now** locks it on demand. To re-lock automatically after some idle minutes, set `TZ_LOCK_AFTER_MIN`.
* **Had a 6-digit PIN before?** It still works: type it in the password box. You can change it in **Settings → Your password**.
* **Typos are forgiven:** a wrong password never blocks your phone's internet address. Wrong passwords across the whole internet are limited to 10 per hour, so nobody can guess it. While that limit is active, Face ID, the code from another phone, and devices already logged in keep working. 8 wrong tries on a locked device log that device out.
* **Other options on the front page:** "Have a code from your other phone?" (Settings → Show a login code) and **Forgot your password?**. That one uses the recovery code from the same Render logs (`TradeZone recovery code`, single use, a fresh one appears after each use and each restart), then you choose a new password.
* **Stored safely:** only a salted scrypt hash is kept. Very common passwords (password, 123456, qwerty…) are refused.
* **Face ID:** uses passkeys. Your phone's secure chip checks your face and signs a one-time challenge. **No face data ever leaves the phone**; the server stores only a public key. Also works with Touch ID, Android fingerprint and face unlock, and Windows Hello.
* **Linking accounts and other important actions** (real money, broker, wallets, sending money, changing your login, adding a device) ask you to confirm it's you, with **one** method at a time: Face ID if you use it, otherwise your password, plus a small link to switch. **Logging in counts as confirming for 10 minutes**, so linking right after you log in asks nothing extra. Each confirmation also lasts 10 minutes.
* **Linking your broker:** the sheet has an **Open Alpaca** button, and if you copy both keys together, one **Paste** fills both boxes.
* **Your data stays put:** if a disk is mounted at `/data` (the Render blueprint does this), the password, trades and everything the bots learned are kept there automatically, so a redeploy doesn't wipe them.
* `TZ_API_TOKEN` is optional: it's only for scripts (`Authorization: Bearer …`), and it also works as a recovery code.

## Settings and linked accounts

Everything is under **Settings** on the home screen, in plain language:

* **Logging in:** change your password, turn on Face ID, show a code for another phone, lock now, log out.
* **Linked accounts:**
  * 🏦 **Real-money account (Alpaca):** tap **Connect** and follow the 4 steps on screen. Paste the Key ID and Secret from your Alpaca *Live* account (📋 Paste buttons included). TradeZone checks the keys with Alpaca and shows your available cash. Keys are stored on your server only and never shown again. Practice-account (paper) keys are recognised and explained. You keep using practice money until you tap **Use real money**.
  * 🤖 **Grok AI helper:** paste your xAI key (see below).
  * 👛 **Crypto wallets:** tap **Add a wallet** and **paste** the address. TradeZone recognises the coin (Bitcoin, Ethereum, Solana, XRP, Stellar, Cosmos, Hedera, EOS), even from `bitcoin:…?amount=` links. For coins that use a tag or memo, it asks "Is this wallet at an exchange?" and, if yes, asks for the tag. Then you give it a nickname. Each saved wallet shows **💸 Send money**: pick the coin, type the amount (with a "≈ $" estimate), confirm with your password or Face ID.
* **Devices logged in:** see your phones and computers and **Log out everywhere**.
* **Advanced:** the security log, blocked addresses and every technical detail are in the **Pro** view.

## Getting a web address for the dashboard

The dashboard is served by TradeZone itself. **It's ready the moment the server starts.** The header shows `● live` once your browser connects. Recent price history is pre-loaded at startup, so the bots can trade within seconds (an Alpaca-only setup shows a 30-bar warm-up banner first).

**On your computer:** run `python -m tradezone serve` and open http://127.0.0.1:8000.

**On the internet (Render, about 5 minutes):**

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, then pick the repo. `render.yaml` sets up an always-on service with a disk for the trade journal.
3. When the deploy turns **Live**, open the `https://tradezone-….onrender.com` URL Render shows. The front page asks for the SETUP code (shown in Render's **Logs** tab), then you pick a password.
4. Optionally add your Alpaca / Grok keys under Environment. Never commit keys.

Any Docker host works the same way (`docker run -p 8000:8000 --env-file .env -v tz-data:/data tradezone`). Choose your password as soon as the site is up, because it controls real-money trading.

## Quick start

```bash
pip install -r requirements.txt
python -m tradezone serve            # dashboard at http://127.0.0.1:8000 (paper, live prices)
```

Fast headless simulation report:

```bash
python -m tradezone sim --steps 3000
```

Run the tests:

```bash
python -m pytest
```

Docker:

```bash
docker build -t tradezone .
docker run -p 8000:8000 --env-file .env -v tz-data:/data tradezone
```

## Configuration

All settings are environment variables with the `TZ_` prefix. A `.env` file in the working directory is loaded automatically. See [`.env.example`](.env.example) for the full list.

| Setup | Variables |
| --- | --- |
| **Default:** live crypto prices (Coinbase), paper fills | nothing |
| Offline demo with synthetic prices | `TZ_FEED=sim` |
| Alpaca paper account | `TZ_FEED=alpaca TZ_BROKER=alpaca TZ_ALPACA_KEY=… TZ_ALPACA_SECRET=…` |
| Paper by default, with one-click live switching | add `TZ_ALPACA_LIVE_KEY=… TZ_ALPACA_LIVE_SECRET=…` and use a real feed (see [Live trading](#live-trading)) |

## Live trading

> **Warning.** Live mode places real orders with real money. The bundled strategies have only been tested on synthetic data, which says nothing about real-world profitability. Only trade money you can afford to lose, and use a dedicated account.

**The desk always starts in PAPER.** It stays there until you switch it yourself.

### One-time setup

**Easiest:** in the app, go to **Settings → Linked accounts → Real-money account → Connect** and paste your Alpaca *Live* API keys. That's all: the **Use real money** button becomes active.

Or, as server settings on Render:

```bash
TZ_ALPACA_LIVE_KEY=...        # your Alpaca *live* account keys (start with AK)
TZ_ALPACA_LIVE_SECRET=...
TZ_LIVE_MAX_CAPITAL=500       # optional: bots trade at most $500 of the account
```

Prices must be live (the default `TZ_FEED=auto` is). Your password protects the switch, so no access token is needed. If something is missing, the **Use real money** dialog says what in plain words, with a **Connect account** shortcut.

### Switching

* **Go LIVE (red button):** opens a dialog that connects to your live account and shows its cash and equity, how much the bots may trade (after `TZ_LIVE_MAX_CAPITAL`), and where the kill switch trips. Type `LIVE` to enable the confirm button. Once live, the page gets a red frame and a `LIVE · REAL MONEY` badge.
* **Back to PAPER (blue button):** one click plus a confirmation. If live positions are open, the desk pauses new entries, closes them at market, waits until the book is flat, switches, then restores its previous pause state.
* Pause, Flatten all and Kill switch work the same in both modes.

### What the switch protects against

| Check | Why |
| --- | --- |
| Live keys configured, real price feed | No orders priced off synthetic data |
| Live account reachable and **flat** | The desk doesn't import existing positions, so it refuses accounts that already hold any |
| Paper book flat (the dialog offers **Flatten paper positions**) | Positions never carry across modes |
| Kill switch not tripped | Investigate first, then reset |
| A password is set before going live on a public address | Nobody else on the network can flip you to live |

Paper and live keep **separate books**, so P&L, trades and the audit ledger never mix, and paper resumes where it left off. In live mode, Portfolio Manager checks positions against the broker every 30 cycles, and any mismatch pauses trading. Wallet deposits are recorded but not added to trading capital (your broker balance is the source of truth), and withdrawals are `manual_required`.

To start directly in live mode (for example, on a server after a restart), set `TZ_MODE=live` and `TZ_LIVE_CONFIRM="I UNDERSTAND THE RISKS"`. If any check fails at startup, the desk stays in paper and raises a critical alert.

## API

The dashboard signs in with an HttpOnly session cookie (`POST /api/login`). Scripts can instead send `Authorization: Bearer <TZ_API_TOKEN>` on every request; Bearer clients skip the app lock. Tokens are never accepted in URLs. Endpoints marked **step-up** also need the header `X-TZ-Confirm: <TZ_API_TOKEN>`.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Liveness, returns `{"ok": true}` only (no auth) |
| POST | `/api/login` · `/api/logout` | `{"token": …}` → session cookie / end the session |
| GET | `/api/auth` | Signed in? Locked? Which unlock methods (password / Face ID) are set |
| POST | `/api/unlock/pin` · `/api/lock` | Unlock with `{"pin": "482915"}` / lock now |
| POST | `/api/webauthn/auth/options` · `/api/webauthn/auth/verify` | Face ID unlock (WebAuthn) |
| GET | `/api/applock` | App-lock status: password set, Face ID keys, lock timeout |
| POST / DELETE | `/api/applock/pin` | Set or change / remove the password (an old 6-digit PIN counts as one) (**step-up**) |
| POST | `/api/webauthn/register/options` (**step-up**) · `/api/webauthn/register/verify` | Turn on Face ID for this device |
| DELETE | `/api/webauthn/passkeys/{id}` | Remove a Face ID key (**step-up**) |
| GET | `/api/security` | Sessions, bans, security log, limits |
| POST | `/api/security/unban` · `/api/security/signout-all` | Unblock an IP / sign out every device |
| GET | `/api/state` | Full desk state: portfolio, bots, orders, events, Executive, Grok status |
| GET | `/api/bots` · `/api/bots/{name}/log` | Bot team status / one bot's last 200 actions |
| POST | `/api/bots/{name}` | `{"enabled": false}` disables a bot (safety-critical bots can't be disabled with positions open) |
| POST | `/api/control/{action}` | `pause`, `resume`, `flatten`, `kill`, `reset` |
| GET | `/api/trades?mode=&symbol=&limit=&offset=` · `/api/trades.csv` | Permanent trade log with totals / as CSV |
| GET | `/api/journal?topic=order.filled` | Raw audit trail |
| GET / POST | `/api/mode` | Preview a paper/live switch / switch: `{"mode": "live", "confirm": "LIVE"}` (**step-up**) or `{"mode": "paper", "flatten": true}` |
| GET · POST · DELETE | `/api/grok` · `/api/grok/config` (**step-up**) | Grok status / save key, model and mode / remove the saved key |
| POST | `/api/grok/ping` | Test the Grok connection |
| GET / POST | `/api/wallets` | List wallets and transfers / link a wallet `{chain, address, tag?, label?, no_tag_confirmed?}` (**step-up**) |
| DELETE | `/api/wallets/{id}` | Unlink a wallet |
| GET | `/api/deposits/info` | Deposit address and deposit tag for each wallet |
| POST | `/api/deposits` · `/api/deposits/{id}/credit` | Record an incoming deposit / attribute a held one |
| POST | `/api/withdrawals` | Withdraw to a linked wallet `{wallet_id, asset, amount}` (**step-up**) |
| WS | `/ws` | Live stream: `snapshot` on connect, then `action` (bot thoughts) and `event` (team events) as they happen, and `state` every cycle. Authorised by the session cookie; scripts send `{"type": "auth", "token": …}` as their first message. |

## Project layout

```
tradezone/
  core/        event bus, models, indicators, portfolio ledger
  bots/        the 11-bot team (incl. Grok AI analyst, the Executive head trader, the Skimmer and the learning Coach)
  wallets.py   wallet linking, deposit/withdrawal tags, funding ledger
  market/      feeds: simulated, Coinbase (public), Alpaca
  brokers/     paper broker, Alpaca broker
  desk.py      orchestrator: cycle loop, controls, kill switch
  server.py    FastAPI REST + WebSocket
  web/         single-file real-time dashboard
tests/         unit, integration, adapter (mocked HTTP) and API tests
```

## Extending

* **New strategy:** add a scoring function to `STRATEGIES` in `bots/quant.py` and weight it in `WEIGHTS`.
* **New bot:** subclass `bots.base.Bot`, subscribe to topics in `subscriptions()`, optionally implement `on_cycle()`, and add it to `TEAM`.
* **New broker or feed:** implement `brokers.base.Broker` or `market.base.MarketFeed`, then register it in `desk.build_broker` / `desk.build_feed`.
