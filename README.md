# TradeZone

A multi-bot trading platform. Twenty-nine specialised bots, including an AI analyst powered by xAI's Grok and a Coach that learns from every trade, each with its own role, work as a desk. They talk over an async event bus, and you supervise them from a real-time web dashboard with pause, flatten and kill-switch controls.

It trades **mainly crypto**, including quick flips on trusted meme coins. It runs in **paper mode by default**, trading **live market prices** with simulated fills and no money at risk. The bots learn on the same data they'll later trade live. Live trading through **Uphold** or Alpaca is supported, but you have to opt in explicitly (see [Live trading](#live-trading)).

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
| **Reporter** 📰 | World and market news | Reads crypto, world and business news and the Fear & Greed index; bad news on a coin blocks buying it, scary world news makes trades smaller (see [News](#news-what-the-world-is-saying)) |
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

**One page per tab (Simple view).** The bottom bar has five tabs, and each is its own page that opens at the top. The phone's back button works, and links like `…/#/trades` open straight to that page.

| Tab | What's on it |
| --- | --- |
| 🏠 **Home** | The bots and all the trading: starship Nova, your balance and the trading buttons, the live profit & loss chart, Today's hunt, open trades, the bot crew |
| 📣 **Activity** | What's happening (live), what the world is saying (news), team rules, what the team has learned |
| 📈 **Trades** | The live profit & loss chart, latest results and the full trade log (filter, export) |
| 💰 **Money** | Real-money account, Grok AI helper, crypto wallets and sending money |
| ⚙️ **Settings** | Logging in (another device, log out), your goal, devices logged in |

The **Pro** view keeps everything on one long, detailed page for experts.

**🎯 Today's hunt** (top of the Home screen) shows what the bots did today: chances checked, how many the team liked, trades opened, and wins out of trades closed. It also lists, in plain words, the most common reasons they skipped, so you can tell the bots are working even when they're not buying. If something is stopping them altogether (no live prices, paused, or emergency stop), the card says so.

**📈 Live profit & loss chart** (Home and Trades). A line shows your profit or loss over time, after all fees. It moves with every price update. Every trade is marked on the line: 🔷 a buy, 🟢 a sale at a profit, 🔴 a sale at a loss. Tap or hover anywhere for the time, the P&L at that moment and any trades there. Pick **15m · 1h · 1 day · 1 week · All**, and switch between **Practice** and **Real money**. Each has its own line, which is never mixed with the other, and real money carries on from one session to the next. Above the chart: the change over that period, trades closed and won, and the fees paid.

**🚀 Starship Nova.** Above your balance, the TradeZone starship flies through space and reacts to how trading is going:

| What's happening | Starship Nova |
| --- | --- |
| Quiet market | Cruising past a ringed planet, stars drifting by |
| Up today | **Thrusters up:** a longer flame, stars stream past faster |
| +1% day, 3+ wins in a row, or the goal reached | **WARP SPEED:** star streaks, the ship glows gold |
| A winning trade closes | **BANKED IT!** A shooting star and a victory loop, the profit in the captain's log |
| Down day or a losing trade | **Took a hit:** an asteroid knocks the ship, red alert ("the stop kept the damage small") |
| Paused or emergency stop | **Shields up:** the ship holds still in a shield bubble and the stars stop |

**Galaxy theme:** deep space with a slowly drifting, twinkling starfield over violet and teal nebulae, glassy panels, and glowing titles. The balance card is a nebula: violet for practice money, teal for real money. A **✨ star power** readout shows your balance, with a meter for today's move and a rank that grows with your total return (Cadet → Pilot → Captain → Commander → Admiral). Your win streak lights up as stars, and winning trades end with a **SUPERNOVA!** burst. It's all CSS and inline SVG, with no outside fonts or images, so the security policy stays strict. Animations switch off if your phone has "Reduce Motion" on.

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

* **Bot floor:** the 29 bots in the order a trade flows through them (Scout → Trend Scout → Reporter → the 10 advisors → Strategist → Quant → Grok → Risk Manager → **Executive** → Executor → Portfolio Manager → Skimmer → Flipper → Sentinel → Coach). The header shows the Executive's stance and decision speed. Each tile shows what the bot is doing right now, how busy it is (actions/min, total), when it last acted, and **lights up in its colour** whenever it acts. Tap a tile (or a bot card) for that bot's full log of its last 200 actions, which keeps updating while open.
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

## News: what the world is saying

The 📰 **Reporter** reads public news feeds every 5 minutes: CoinDesk, Cointelegraph, Decrypt, and BBC World and Business, plus CNBC markets (`TZ_NEWS_FEEDS` to change them). It also reads the crypto **Fear & Greed index**. No account or key is needed. Each headline is scored with a transparent word list (for example "hack", "lawsuit" and "rate hike" are negative; "ETF approval", "rate cut" and "record high" are positive) and matched to the coins it mentions.

* **Bad news on a coin in the last 2 hours** (a hack, a delisting, a lawsuit): no new buys of it.
* **Conviction nudge:** a trade with the news gets up to +0.1 conviction, one against it up to −0.1.
* **Scary world news** (very negative market mood): new buys are half-size.
* **Grok** (if connected) sees the recent headlines and the market mood when it reviews a trade.

News can only make the team more careful or slightly more confident. It never opens a trade, never moves a stop and never touches the loss limits. It is off when the bots trade synthetic demo prices. The Home screen's "What the world is saying" card shows the mood, Fear & Greed and the latest headlines, with links.

## Your goal, and "don't lose my money"

**🚀 The goal: grow the balance rapidly with minimal losses.** This is the 5th team rule. Growth comes from compounding many quick, fee-paid wins (quick flips on real money, skims, runners, and shorts where they work); minimal losses come from three guards that sit on top of the hard limits below:

| Guard | What it does |
| --- | --- |
| A small stop on every trade | each trade risks about 1.5% of the balance (less for meme coins), longs and shorts alike |
| Losing-day brake | once the day is down half the daily limit (1.5% with real money), new trades are **half size** (`TZ_LOSING_DAY_SIZE`) |
| Day lock | once the day has been up **2%** (`TZ_DAY_LOCK_TRIGGER_PCT`), giving back **half** of the day's best gain (`TZ_DAY_GIVEBACK_PCT`) makes new trades **half size** (`TZ_DAY_LOCK_SIZE`) until it recovers. A good day never stops the bots: there's no ceiling on the upside. (`TZ_DAY_LOCK_STOPS=true` brings back "no new trades until tomorrow".) |

**💵 The daily goal: a $1,000+ day with the money on hand** (`TZ_DAILY_GOAL_USD`, or **Settings → Your goal**; 0 turns it off). The goal card shows today's profit against it. It's a floor, not a ceiling: reaching it never stops the bots. If a goal day starts slipping (a quarter of the day's best given back, `TZ_DAILY_GOAL_GIVEBACK`), new trades are half size until it recovers. The bots never bet bigger to force it. The card also says plainly how big the goal is for your balance: $1,000 is 1% of $100,000, but 2,000% of $50 in a single day, which no amount of risk can make reliable (at about 1% a day, a $1,000 day needs roughly a $100,000 balance).

The goal card (Home) shows the milestone tracker (default **$50 → $10,000**, change it in **Settings → Your goal**), the pace so far, and which guard is active. Growing fast never means bigger bets than the limits allow: compounding makes each trade a little bigger as the balance grows, and the Coach sizes up slowly after good results and down after bad ones. Be realistic: $50 → $10,000 is a 200× increase, about 530 days at a steady +1% a day or 270 at +2%.

**🛡️ Don't lose the money** is the 6th team rule. Real money risks more per trade (with patient exits), so its loss limits are set to match:

| Protection | Practice | Real money |
| --- | --- | --- |
| Stop for the day after a loss of | 5% | **5%** (`TZ_LIVE_MAX_DAILY_LOSS_PCT`; Aggressive 10%) |
| Stop everything after a drop from the peak of | 15% | **15%** (`TZ_LIVE_MAX_DRAWDOWN_PCT`; Aggressive 30%) |
| Stop everything if the account falls below what you put in by | – | **15%** (`TZ_LIVE_CAPITAL_FLOOR_PCT`; Aggressive 35%); only you can restart it |
| Stop-loss on every trade; at risk per trade ≤1.5% practice / 2.5% real money (4% Aggressive); ≤7% / 10% across all trades | ✓ | ✓ |
| Optional cap on how much of the account the bots may use | – | `TZ_LIVE_MAX_CAPITAL` |

**No trading system can promise you won't lose money.** Prices can jump straight through a stop, and a run of losing trades is normal. These rules keep losses small and stop the bots before a bad stretch becomes a big one. Only trade money you can afford to lose.

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

* **Long:** below the lowest low of the last 10 bars (the last dip buyers defended), minus a 1×ATR buffer so ordinary noise doesn't tag it.
* **Off round numbers:** if the stop lands on or near a round number (100, 105, 65,000…), where stops cluster and get hunted, it moves just beyond it.
* **Never too tight:** at least 1.5×ATR from entry.
* **Never too wide:** if the swing stop is more than 4×ATR away, the trade is **skipped** rather than taken with oversized risk.
* **Target:** 2× the stop distance (2:1 reward-to-risk). The trade is skipped if resistance is less than 1R away, because it would have no room to work.
* **Fallback:** if there's no swing level yet, the stop is 2×ATR. `TZ_STOP_METHOD=atr` switches everything to plain ATR stops.

Risk management layered on top:

| Rule | Default |
| --- | --- |
| Risk per trade: position sized so hitting the stop loses at most this | 1.5% of equity (half that in volatile regimes and for meme coins) |
| Portfolio heat: total loss if *every* open stop is hit | 7% of equity |
| Max position size / total exposure / open positions | 25% / 100% / 6 |
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

**Easiest:** tap the **Grok** button (or the GROK badge) in the dashboard, paste your xAI API key, choose the mode, and tap **Save & connect** (on a logged-in device). The key is stored on your server only, shown afterwards as `…abcd`, and never sent back to the browser. It survives restarts, and it overrides `TZ_GROK_API_KEY` if both are set.

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

Link wallets under **Settings → Crypto wallets** or via the API. **20 networks:** Bitcoin, Ethereum, Base, Arbitrum, Optimism, Polygon, BNB Smart Chain, Avalanche C-Chain, Solana, Tron, Litecoin, Dogecoin, Cardano, Polkadot, Algorand, XRP Ledger, Stellar, Cosmos, Hedera and EOS. Each shows its typical network fee, so you can pick a cheap one: USDC on Base, Solana or Polygon costs cents, while Ethereum can cost dollars.

* **Real checksum checks:** a typo is the most common way people lose crypto, and most address formats have a built-in checksum. TradeZone verifies it: Base58Check (Bitcoin legacy, Litecoin, Dogecoin, Tron, XRP), Bech32/Bech32m (Bitcoin SegWit and Taproot, Litecoin, Cosmos, Cardano), EIP-55 (all Ethereum-style networks), Stellar CRC16, Polkadot SS58 and Algorand. Solana, Hedera and EOS addresses have no checksum, and the app says so.
* **The right network:** an Ethereum-style 0x address is valid on every EVM network, and sending on the wrong one can lose the money. The app asks which network your wallet uses.
* **Test send first:** the first send to a new wallet is capped at **$25** (`TZ_TEST_SEND_USD`). When it arrives, tap **I got it** and full amounts unlock. A checksum proves an address is well-formed, not that it's yours; the test send proves that.
* **Withdrawal tags and memos:** XRP destination tags and Stellar, Cosmos, Hedera and EOS memos are validated and attached to every withdrawal. If you leave the tag empty on a tag chain, you must confirm the destination doesn't need one, because a missing tag is the classic way to lose exchange deposits.
* **Deposit tags:** every linked wallet gets a unique numeric deposit tag. Deposits that carry it are credited to that wallet. On chains without tags, deposits are matched by sender address. Unmatched deposits are **held** for review and credited only when you attribute them (`POST /api/deposits/{id}/credit`). A transaction hash can't be credited twice.
* **Withdrawal safety:** withdrawals go only to linked wallets, only after a 24-hour cooldown on new wallets (`TZ_WITHDRAWAL_COOLDOWN_H`), within a rolling 24-hour limit (`TZ_MAX_DAILY_WITHDRAWAL`), and never above free cash.
* **Funding isn't P&L:** deposits and withdrawals move the P&L baselines, so returns and the kill switch aren't distorted.
* **Deposit addresses:** set the desk's own receiving addresses with `TZ_DEPOSIT_ADDRESSES=XRP:r...,ETH:0x...`. They're checksum-checked too.

> **Paper vs live, and cashing out real money.** In paper mode, deposits and withdrawals move simulated cash. In live mode, your money sits at your broker (Alpaca). To cash out real money as crypto, sell to cash or USDC in TradeZone, then withdraw from Alpaca's own app to a wallet you linked and **test-sent** here. TradeZone records it as `manual_required` and doesn't send real crypto on its own. Every crypto network charges a transfer fee, so a withdrawal is never entirely free; picking a cheap network (USDC on Base, Solana or Polygon) keeps the fee to cents.

## Security

The site controls real money, so it's locked down in layers. Each is tested in `tests/test_security.py`, including simulated attacks from a malicious site.

| Threat | Protection |
| --- | --- |
| Someone finds your site | Nothing works without logging in. A device logs in only with the one-time code printed in the server logs, or a 6-digit code (or one-tap link) made on a device that's already logged in. Wrong codes ban the guessing address, the log code rotates after 10 wrong tries, and pairing codes burn out after 5. An optional `TZ_API_TOKEN` must be at least 16 characters on a public address, or the server refuses to start. `/api/health` reveals nothing, and the API docs are switched off. |
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

Also sent: HSTS, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, cross-origin isolation headers, `Cache-Control: no-store` on the API, and no `Server` header. If you think someone else got in, use **Settings → Log out everywhere**. If you use an access token for scripts and suspect it leaked, change `TZ_API_TOKEN` on Render and redeploy.

### Logging in: one code, no password, no Face ID

Every visit starts on the **front page** with **one box**: type a login code.

* **First time (or every device lost):** open Render → your service → **Logs** and find the line `TradeZone login code: 1234-5678`. Type the 8 numbers. A fresh code appears after each use and each restart.
* **Another phone or computer:** on a device that's already logged in, open **Settings → Log in another device**. It shows a **6-digit code** (works once, for 5 minutes) and a **one-tap link** you can send yourself. Open the link on the other device and it's logged in.
* **You stay logged in for a year** on each device, including the home-screen app. **Log out** in Settings, or **Log out everywhere** if a device is lost.
* **Money safety without passwords:** real money only starts after you type **LIVE**. A new withdrawal wallet waits 24 hours and must pass a small **test send** before full amounts. Withdrawals go only to linked wallets, within a daily limit.
* **Your data stays put:** if a disk is mounted at `/data` (the Render blueprint does this), your logins, trades and everything the bots learned survive redeploys.
* `TZ_API_TOKEN` is optional: it's only for scripts (`Authorization: Bearer …`), and it also works as a login code.

## Settings and linked accounts

Everything is under **Settings** on the home screen, in plain language:

* **Logging in:** log in another device (code or link), log out, log out everywhere.
* **Your goal:** change the starting amount and target.
* **Linked accounts:**
  * 🏦 **Real-money account (Uphold or Alpaca):** tap **Connect**, pick where your money is, and follow the steps. For Uphold, paste the access token (see [Trading real money on Uphold](#trading-real-money-on-uphold)). For Alpaca, follow the 4 steps on screen. Paste the Key ID and Secret from your Alpaca *Live* account (📋 Paste buttons included). TradeZone checks the keys with Alpaca and shows your available cash. Keys are stored on your server only and never shown again. Practice-account (paper) keys are recognised and explained. You keep using practice money until you tap **Use real money**.
  * 🤖 **Grok AI helper:** paste your xAI key (see below).
  * 👛 **Crypto wallets:** tap **Add a wallet** and **paste** the address. TradeZone checks it against the network's real checksum and recognises the network (20 supported, see below), even from `bitcoin:…?amount=` links. Ethereum-style 0x addresses work on several networks, so it asks which one. For coins that use a tag or memo, it asks "Is this wallet at an exchange?" and, if yes, asks for the tag. Then you give it a nickname. Each saved wallet shows **💸 Send money**: pick the coin, type the amount (with a "≈ $" estimate), and the first send to a new wallet is a small test.
* **Devices logged in:** see your phones and computers and **Log out everywhere**.
* **Advanced:** the security log, blocked addresses and every technical detail are in the **Pro** view.

## Getting a web address for the dashboard

The dashboard is served by TradeZone itself. **It's ready the moment the server starts.** The header shows `● live` once your browser connects. Recent price history is pre-loaded at startup, so the bots can trade within seconds (an Alpaca-only setup shows a 30-bar warm-up banner first).

**On your computer:** run `python -m tradezone serve` and open http://127.0.0.1:8000.

**On the internet (Render, about 5 minutes):**

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, then pick the repo. `render.yaml` sets up an always-on service with a disk for the trade journal.
3. When the deploy turns **Live**, open the `https://tradezone-….onrender.com` URL Render shows. The front page asks for the login code (shown in Render's **Logs** tab). That's it.
4. Optionally add your Alpaca / Grok keys under Environment. Never commit keys.

Any Docker host works the same way (`docker run -p 8000:8000 --env-file .env -v tz-data:/data tradezone`). Log in as soon as the site is up (the first device to log in becomes the owner), because it controls real-money trading.

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

Real money trades through **Uphold** (crypto and meme coins), **Alpaca** (stocks + crypto) or **Kraken** (crypto, including BONK, WIF and FLOKI, which Alpaca doesn't list). Connect one on the **Money** page. The one you connected last is the one that trades.

### Kraken: trades and profits, never withdrawals

The bots can **buy, sell and take profits** on Kraken. **Nothing in TradeZone can withdraw** money or crypto from it, enforced three ways:

1. **The code can't.** Every private Kraken request goes through one function that only allows `Balance`, `TradeBalance`, `AddOrder`, `CancelOrder`, `QueryOrders`, `OpenOrders` and `WithdrawInfo`. (`WithdrawInfo` only *reads* information and is used for the check below.) `Withdraw`, wallet transfers, staking and everything else are refused before any request is sent.
2. **The key can't.** When you connect, TradeZone checks the key and **refuses one that has the "Withdraw Funds" permission**, even though it would never use it. It also checks the key can read balances and place orders, and explains which switch to turn on if not.
3. **Your coins stay yours.** When live trading starts, TradeZone notes what you already hold on Kraken. It only ever trades what the bots buy themselves, so coins you had before are never sold, and they don't block going live.

Make the key in Kraken → **Settings → API → Create API key**. Turn on **Query Funds**, **Query Open Orders & Trades**, **Query Closed Orders & Trades**, **Create & Modify Orders** and **Cancel/Close Orders**. Leave **Withdraw Funds**, Deposit Funds and Earn **off**.

Orders are market orders on each coin's USD pair, rounded down to Kraken's lot size and checked against its minimum order. Kraken's fee comes back with every fill and is counted in your results. The bots trade with the USD in the account (not USDC or USDT). Server-side alternative to linking in the app: `TZ_LIVE_VENUE=kraken`, `TZ_KRAKEN_KEY`, `TZ_KRAKEN_SECRET`.

### When real money isn't trading

A few things used to stop real-money trading quietly. They're fixed:

* **One worse-than-expected fill paused everything.** A market buy fills at the *ask* (the seller's price), and on a coin with a wider gap between buy and sell prices that can be well over the 0.5% slippage limit. Now the expected price for a real-money order is Kraken's ask (bid when selling), and a fill beyond the limit only **cools that coin off for 30 minutes**. Real integrity problems (wrong coin, wrong side, overfill) still pause everything.
* **Pauses that never ended.** A pause because the books didn't match the broker **clears itself** as soon as they match again; a pause left over from an interrupted mode switch, or from an old bad fill, is cleared on restart.
* **The records' check paused trading over normal things.** Every 30 seconds the Portfolio Manager compares the bots' records with what the account really holds. It used to pause trading on *any* difference. Now:
  * a difference must show up on **two checks in a row**, with nothing in flight and no fill in the last 20 s (a broker's balance can lag a fill by a few seconds);
  * **more** in the account than the bots bought (Kraken Rewards interest, coins you bought or deposited yourself) is noted as **yours** and left alone, and remembered across restarts;
  * **less** than the bots' records (you sold or moved the bots' coins outside TradeZone) **updates the records** as a sale at the market price, with a warning, and trading continues;
  * only if the records need correcting **3 times within an hour** does trading pause, because then something really is wrong;
  * after a restart, live trading resumes even if the account changed while TradeZone was off; the same check sorts it out.
* **Dust that blocked trading.** Selling a whole position on Kraken could leave one lot behind (e.g. 0.00000001 XRP), because the order size was rounded with floating-point maths. That leftover was below Kraken's minimum, so its exit was refused on every tick, and it still counted as an open trade: it blocked new trades on that coin and filled the open-position and "same direction" slots. Sizes are now rounded exactly; any leftover below the venue's minimum is taken off the bots' books (it stays in your account as yours); and a refused exit waits 30 s before it's tried again.
* **One coin Kraken won't quote.** Kraken rejects a whole price request if a single pair is unknown (just delisted, say), which left every coin without a price. The bad pair is now found and dropped, and the rest stay priced.
* **Fill check.** Right before each real-money order the bots fetch Kraken's bid and ask, so the "was this a bad fill?" check compares against what a market order really gets.
* **Orders that fill late.** An order still open after 60 s is cancelled, and the bots now check it once more afterwards, so a fill that arrived meanwhile is always booked (a lost fill used to cause a mismatch later).
* **Kraken Rewards (auto-earn).** Kraken books coins and USD in Rewards as `XBT.F`, `USD.F` and so on. These are now counted as yours, so the bots' records match the account instead of pausing for a mismatch.
* **Small accounts.** Kraken's minimum order (about $5 of most coins) is bigger than a 1%-risk meme-coin trade on $50. Such buys are now rounded up to the minimum when that's still safe (see the table below), and transfer fees no longer block new trades.
* **Skims on real money.** With Kraken's 0.40% fee, a +2.5% skim couldn't be reached inside an hour. Real-money skims now aim for **+1.5% after fees** within 2 hours.
* **No US dollars.** The bots trade USD pairs. If your Kraken account holds only USDC or USDT, *Use real money* now says so and asks you to convert some to USD.

If the bots still aren't trading, the **🎯 Today's hunt** card on Home lists the most common reasons they skipped, in plain words.

### 🧑‍🤝‍🧑 15 helper bots

Ten **advisors** look at one thing each, and the Risk Manager consults all of them before every *new* trade. Any of them can veto it (with a plain reason); their conviction nudges add up (at most ±0.15) and their size factors multiply (kept between 0.4× and 1.5×). Every hard limit still applies on top, and exits are never affected.

| Bot | Looks at | What it does |
| --- | --- | --- |
| 🌦️ Market Weather | Bitcoin | no new buys in other coins while BTC is falling 1.5%+ in 15 bars (or 3× its normal 15-bar move, in a jumpy market); a small boost when BTC is strong |
| 📏 Volatility Sizer | each coin's volatility vs its usual | trade size 0.6×-1.4× (smaller when unusually wild) |
| 🏅 Momentum Ranker | every coin's 20-bar move | top third: +0.05 and 1.1×; bottom third: -0.03 and 0.85× |
| 🪢 Overextension Guard | distance from the slow average, RSI | won't chase: vetoes buys 8+ ATR above the average or at RSI 90+ |
| 🧱 Resistance Mapper | the nearest recent high | trims a buy (0.8×) when a ceiling sits within half the target |
| 🧩 Correlation Officer | how coins move together | vetoes a buy that moves almost exactly (0.85+) like 2+ coins already held; 0.75× for one |
| 🐋 Whale Watcher | volume vs price | 3× volume with a flat price = accumulation: +0.05 for 10 bars |
| 💧 Liquidity Guard | volume, the bid/ask gap | no buys when volume is under a quarter of usual; 0.7× when the gap is over 0.8% |
| 🧯 Flash-Crash Guard | market-wide shocks | 3+ coins moving 5+ standard deviations in the same bar, or BTC -3% in 5 bars (or 4× its normal 5-bar move): new trades wait 15 min |
| 🔭 Big-Picture Trend | 15-minute candles | against the bigger trend: -0.03 and 0.8×; with it: +0.03 |

One new **strategy**:

* 🎣 **Pullback Hunter**: marks breakouts above the recent high; if within 8 bars the price dips back to that level and holds above it with momentum and normal volume, it proposes a buy (the classic breakout retest).

Four **operations** bots:

* 🧾 **Fee Auditor**: measures the fee actually paid on real-money fills; after 10 it sets the fee the bots plan with to what was really paid (maker orders and fee tiers make it lower).
* 🎯 **Slippage Tracker**: a coin whose fills average 0.3%+ worse than expected over 5 fills trades at 0.6×.
* 📡 **Exchange Health**: 5+ orders rejected by the exchange within 10 minutes: new trades wait 10 minutes (exits continue).
* 📒 **Accountant**: a daily report when each UTC day ends (profit after fees, fees, wins, best and worst trade), kept for a week.

Each one can be switched off on the bot floor (Pro view).

### 🔥 The Trend Scout: every meme coin worth trading, and what's trending

The 14th bot plugs the team into the coins where the action is. Every **30 minutes** (`TZ_DISCOVERY_EVERY_MIN`) it reads CoinGecko's free public market data (no account needed):

* **meme coins** with a market cap of at least **$50k** (`TZ_DISCOVERY_MIN_MCAP`) and **promising volume**: at least **$100k** traded in 24 h (`TZ_DISCOVERY_MIN_VOLUME`) and at least **5%** of the market cap changing hands a day (`TZ_DISCOVERY_MIN_TURNOVER`), so dead coins are skipped;
* the coins **trending** on CoinGecko right now.

Every one of them that the exchange lists against USD (with Kraken prices) is added to what the bots watch and trade, trending coins first, up to 20 (`TZ_MAX_DISCOVERED`), with recent price history loaded so the bots can act at once. Meme coins get the meme rules (smaller bets, wider stops), trending coins a small conviction boost (`TZ_TRENDING_NUDGE`). Coins that stop qualifying are dropped again, never while the bots hold one. **Today's hunt** on Home lists what's trending. `TZ_DISCOVER_COINS=false` turns it off.

Two limits keep this safe: a coin is only **bought** if the "never buy what can't be sold" checks pass (listed for buying and selling, a tight bid/ask gap, enough daily trading for the position), so a thin coin is watched but not bought. And coins that **only trade on decentralised exchanges** (most $50k-$1M meme coins) can't be bought: TradeZone trades through the exchange account, and DEX-only tokens carry a high risk of tokens that can never be sold.

### Kraken's meme coins

With a Kraken account (and `TZ_FEED=auto`, the default), prices come from **Kraken itself**, so every coin the bots can buy there has a price, and practice matches real money. At start-up TradeZone reads Kraken's list of USD pairs and:

* keeps the usual coins (BTC, ETH, SOL, ... and the meme coins DOGE, SHIB, PEPE, BONK, WIF, FLOKI) that Kraken lists, and skips the rest;
* adds other well-known meme coins **Kraken lists against USD** (TRUMP, PENGU, FARTCOIN, POPCAT, MEW, BOME, TURBO, PNUT, ...) if they trade at least **$1M a day** (`TZ_MIN_MEME_VOLUME_USD`), the busiest first, up to 12 meme coins in all (`TZ_MAX_MEME_COINS`). They get the meme-coin rules: smaller bets and wider stops. `TZ_KRAKEN_MEMES=false` turns this off.

### Never buys a coin it can't sell

Before every real-money buy (and in practice too when a real account is linked), the Risk Manager checks the coin can be **sold again**:

| Check | Refused when |
| --- | --- |
| Listed for buying *and* selling | Kraken doesn't offer the USD pair, or it's paused / cancel-only / reduce-only (the pair list is refreshed every 15 min) |
| Big enough to sell | the amount is under Kraken's minimum order size, or worth under 1.25× its minimum order value (room for a fall to the stop). On a small account, a buy just under the minimum is **rounded up** to it when that stays inside the position cap and risks at most 1.5× a normal trade |
| Someone to sell to | the bid/ask gap is over **1.5%** (`TZ_SELL_MAX_SPREAD_PCT`), the coin trades under **$250k a day** (`TZ_SELL_MIN_VOLUME_USD`), or the position would be over **1%** of a day's trading (`TZ_SELL_MAX_VOLUME_SHARE`) |
| Fresh data | bid/ask is over 3 minutes old (refreshed every 30 s): real money refuses rather than guesses |

Orders also ask Kraken to take its fee in **USD** (`oflags=fciq`), so every coin bought arrives in full and the whole amount can be sold. A partial profit-take that would leave a part or a remainder under Kraken's minimum sells the whole position instead, so no unsellable leftovers are ever left behind.

### Transfer fees and profit-taking

Moving profit out (to a wallet or the bank) costs a transfer fee. TradeZone estimates it from your **cheapest linked wallet's network** (e.g. XRP or Stellar about $0.10-0.20, Solana ~$0.30, Bitcoin ~$5), or uses `TZ_CASHOUT_FEE_USD` if you set it (else $1). Then:

* every **profit-taking sale** (banking a slice, selling into a spike, a quick flip, the clock closing a stale winner, a strategy exit) must make at least **10%** of one transfer fee after trading fees (`TZ_TRANSFER_FEE_SHARE`), so ten wins pay for one transfer out. This is capped at **0.1% of your balance**: a fee is paid once per cash-out, not per trade, so on a small account it never stops the bots from banking small wins;
* the **Money** page shows *Profit to take home*: what the bots banked, and what you'd keep after one transfer out, and tells you to wait until it's about 10× the fee so the fee stays a small share;
* sending money out shows the fee and how much arrives, and refuses amounts the fee would mostly eat (under 2× the fee).

Stop-losses never wait for any of this: protection always comes first.

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

Prices must be live (the default `TZ_FEED=auto` is). You must be logged in, and you type LIVE to confirm, so no access token is needed. If something is missing, the **Use real money** dialog says what in plain words, with a **Connect account** shortcut.

### Trading real money on Uphold

Uphold is a good fit for a small account (like the $50 goal): you trade straight from your Uphold USD balance into BTC, ETH, SOL, XRP, DOGE, SHIB, PEPE and more.

1. Put the money the bots may use in your Uphold **USD** balance.
2. Make an access token. On the machine running TradeZone, run `python -m tradezone uphold-token` and log in when asked. Your email, password and 2-step code go only to `api.uphold.com` over HTTPS and aren't saved. If Uphold won't make a token this way for your account, any Uphold OAuth access token also works.
3. In the app, go to **Settings → Linked accounts → Real-money account → Connect**, choose **Uphold** and paste the token. Or set `TZ_UPHOLD_TOKEN=...` on the server.
4. Tap **Use real money** when you're ready.

How it works and what to expect:

* **Prices** still come from Coinbase's real-time stream. **Orders** are Uphold transactions between your cards (USD → DOGE to buy, DOGE → USD to sell), committed at once. The first trade in a coin opens a "TradeZone DOGE" card for it.
* **Cost:** Uphold has no separate fee; its cost is the *spread* built into its price, roughly 1% each way on crypto (often more on small coins). Every fill reports the market price and books the spread as a cost, so P&L includes every cent Uphold keeps. When Uphold is your real-money account, practice trades charge the same cost (`TZ_UPHOLD_FEE_BPS`, default 100 = 1% per side; set `TZ_CRYPTO_FEE_BPS` to override).
* **That means fewer, bigger trades.** A round trip costs about 2%, and a trade must aim for at least 3× its cost, so tiny 2.5% skims are skipped and the team leans on meme-coin flips with 15% targets. Rapid trading that can't beat the spread just hands money to the exchange.
* **Coins Uphold doesn't list** stay practice-only; the Connect screen lists them, and the Risk Manager explains instead of sending an order that would fail.
* **Your other coins are left alone.** Only coins in the bots' list count as "already holding positions"; everything else in your Uphold account is ignored. Trading capital is your USD balance (capped by `TZ_LIVE_MAX_CAPITAL` if set).
* `TZ_LIVE_VENUE=auto` (default) uses Uphold when an Uphold token is set, otherwise Kraken when a Kraken key is set, otherwise Alpaca.

### ⏱️ The Flipper: flip trades and the best times to flip

The 13th bot does two jobs:

1. **Learns the best times to flip.** Every closed trade is filed under the hour (UTC) it was opened in: how many, how many won, and the profit after fees. Once an hour has **12+ trades** (`TZ_FLIP_MIN_SAMPLES`), it's rated:
   * **good** (made money and won at least half): trades opened then are **1.25×** bigger (`TZ_FLIP_GOOD_HOUR_SIZE`) and need a little less conviction;
   * **bad** (lost money and lost most trades): trades are **0.6×** the size (`TZ_FLIP_BAD_HOUR_SIZE`), need more conviction, and the Flipper doesn't open its own flips.

   The **What the team has learned** card shows the current hour's rating and the best and worst hours. What it learns is saved across restarts.
2. **Finds flips itself:** a move up on **2× the usual volume** (`TZ_FLIP_VOLUME_RATIO`) with momentum behind it, the price above its fast average and the RSI between 50 and 70 (not yet overbought). It goes through Risk and the Executive like any trade, and the real-money plan, profit slices and moving take-profit apply. `TZ_FLIPPER=false` turns it off.

### 🪙 Accumulating coins: sell a slice high, buy it back cheaper

On a coin the bots hold, the Flipper also works to **own more of it**:

1. When the price is **3% above** what the bots paid (`TZ_ACC_RISE_PCT`) and selling is a profit after fees, it sells a **20% slice** (`TZ_ACC_SLICE`).
2. When the price dips **2.5% below that sale** (`TZ_ACC_DIP_PCT`), it buys the slice back, **only if that gets at least 0.5% more coins than were sold** after both fees (`TZ_ACC_MIN_GAIN`). The coins go back into the same position, which keeps its stop and target.
3. If no dip comes within **48 h** (`TZ_ACC_TIMEOUT_H`), or the trade closes first, the slice simply stays sold, as **profit in dollars**. It never buys back at a higher price, because that would end with fewer coins.

So every *completed* round ends with more coins. Not every round completes, though: when a coin keeps rising, the slice was sold early (as profit). The **What the team has learned** card shows the coins gained and any slice waiting to be bought back. `TZ_ACCUMULATE=false` turns it off.

In simulated real-money trading (random prices, Kraken fees), 2-6 rounds per run completed (for example +8.5 DOGE), 5-9 stayed as dollar profit, and the account ended about where it would have without it (about -$11 across four $500 runs, the extra fees). Real markets that swing back and forth suit it best.

### 💰 Reinvesting what the flips make

Trade sizes are always a share of the current balance, so profits compound by themselves. On top of that, **a quarter of the profit banked today** (`TZ_REINVEST_SHARE`) is added to the money each new trade may risk, so flipping profits go straight back to work the same day. It switches off on a losing day (the losing-day brake), and every trade still has its stop and the usual position and portfolio caps.

### No ceiling on profits

Nothing caps how much the bots can make in a day or on a trade:

* winners keep running: the **moving take-profit** (below) has no top, and a runner has no target at all;
* only a **slice** is banked early: a quarter at a quick flip (`TZ_FLIP_BANK_FRACTION`), a third at the target (`TZ_RUNNER_SELL_FRACTION`); the rest keeps going. A slice smaller than the venue's minimum order is raised to the minimum, or skipped if the rest would then be too small to sell (the whole trade keeps running, protected by its stop), so a small account's winners are never sold off whole by a profit-take;
* a great day **never stops trading**, not at +2%, not at the daily goal, not at +1,000%; if a good day starts slipping, new trades just get smaller;
* trade sizes **compound**: each trade is a share of the current balance, so they grow as it grows.

What stays capped is the *downside*: a stop on every trade, the risk per trade, and the daily-loss, drawdown and capital-floor limits. No code can make 1,000% days happen. How much is made depends on the market. This only makes sure that when the market gives a big move, the bots don't cut it short.

### Moving take-profit: past +10%, the target moves out

Once a trade is **10% past its buy price** (`TZ_TP_STEP_PCT`), it isn't sold at its old target. Instead:

* the **take-profit moves another 10% out** (to +20% after fees), and
* the **stop moves up to lock in half of the step** (+5% at +10%).

This repeats at +20% (target +30%, stop locks +10%), +30%, and so on, so a strong move keeps running while more and more of the profit is locked in. The trade ends when the price pulls back to the locked stop (or the trailing stop, whichever is closer). The Skimmer still banks slices into spikes along the way. Shorts work the same way, mirrored. A runner (no target) only gets the stop steps. Skims keep their own small target. `TZ_TP_STEP=false` turns it off; `TZ_TP_STEP_LOCK` sets how much of each step is locked (0.5).

### Everything on with real money

Every feature runs with real money. Each can be switched off on its own:

| Feature | Real money | Switch |
| --- | --- | --- |
| Quick flips (more trades, smaller targets after fees) | on | `TZ_LIVE_QUICK_FLIPS` |
| Stop-loss 33% from the buy price | on | `TZ_WIDE_STOP` |
| Patient exits (cut early only when momentum and volume say no comeback) | on (and in practice whenever the -33% stop is on) | `TZ_PATIENT_EXITS` |
| Moving take-profit past +10% | on | `TZ_TP_STEP` |
| Quick-flip slice (a quarter banked at +1.2% after fees) | on | `TZ_FLIP_BANK_FRACTION` (0 = off) |
| Skims (small dip-buy day trades) | **off**: they lost money in every simulated run (see below) | `TZ_FLIP_MAX_SKIMS` (4 to turn on) |
| Flipper: best hours to flip, volume flips, accumulating coins | on | `TZ_FLIPPER`, `TZ_ACCUMULATE` |
| Reinvesting today's profit | on (a quarter) | `TZ_REINVEST_SHARE` |
| Growth ladder (smaller coins first) | on | `TZ_FOCUS_SMALL_COINS` |
| Kraken meme coins and small altcoins | on (with Kraken prices) | `TZ_KRAKEN_MEMES`, `TZ_MAX_SMALL_COINS` |
| Daily goal ($1,000, a floor not a ceiling) | on | `TZ_DAILY_GOAL_USD` |
| Shorting | only where the account can (Alpaca margin, stocks) | `TZ_SHORTING` |
| "Never buy what can't be sold" checks | on | (always) |
| Aggressive risk level | opt-in | Settings → Your goal |

Patient exits, skims and the quick-flip slice had been switched off after the losing-trades review below. They're back on by request. With the -33% stop, patient exits are what cuts a falling trade early (when momentum and heavy volume both point down) instead of riding it to -33%. In simulated real-money trading (random prices, Kraken fees), switching all three back on made more trades and left almost no money stuck in losing positions, and ended about 1% lower over four $500 runs (the extra fees). That's within the noise of random prices; real markets will tell.

### Cheaper buys: waiting at the bid (maker orders)

Fees were the biggest cost in every review of the bots' trades. So with real money on Kraken, a buy is first placed as a **post-only limit order at the bid** (`TZ_MAKER_ENTRIES`): it pays Kraken's **maker** fee (0.25% at the starting tier instead of the 0.40% taker fee) and buys at the bid instead of the ask, saving about half the bid/ask gap too. If it isn't filled within **20 seconds** (`TZ_MAKER_WAIT_S`), or Kraken refuses it because it would have bought straight away, the rest is bought at market, so no trade is missed. Exits, stop-losses and profit-taking always sell at market, so they are never delayed.

### Fewer trades lost after a losing streak

Two protections used to sideline the bots for long after a few losses:

* the **Coach's bench** (a coin with 5+ trades, a poor win rate and a net loss sits out) is now **45 minutes** (`TZ_COACH_BENCH_MINUTES`, was 2 hours);
* the **Executive's defensive stance** (after 3 losses in a row: half-size trades) now needs only **0.10 more conviction than usual** (`TZ_EXEC_DEFENSIVE_EXTRA`; with real money about 0.32 instead of a fixed 0.45, which held back nearly every idea), and a losing streak stops counting **20 minutes** after its last loss (was 30).

### Which trades were losing (second review)

Every simulated real-money position (Kraken fees, four $500 runs) was traced from the strategy that opened it to how it ended:

| Opened by | Positions | Won | Total |
| --- | --- | --- | --- |
| Skims | 8 | 2 | **-$16.28** (lost in every run) |
| Quant trend trades | 85 | 39 | -$4.53 |
| Pullback Hunter | 42 | 22 | -$1.73 |
| Snipes (meme breakouts) | 23 | 13 | +$1.88 |

Skims risk their tight 1.2% stop plus ~0.9% of round-trip fees to make 1.5%, so they need to win about 58% of the time just to break even, and they didn't. **Real-money skims are off** (`TZ_FLIP_MAX_SKIMS=0`; practice keeps them). That improved the four runs from $1,973.90 to $1,985.64. Also tested and left as they are, because the differences were within the noise: stricter or looser patient exits, holding a recovering loser for +1% instead of +0.3%, and dropping the quick-flip slice. What's left is mostly fees: the simulated prices are random, so no setting can beat them. Real markets will show whether the entries have an edge.

### Stop-loss at -33% from the buy price

Every trade's stop-loss sits **33% below the buy price** (`TZ_WIDE_STOP_PCT`; 33% above for a short), so ordinary dips, even a 20-30% drop, never stop a trade out. Three things keep this from risking more money:

* **Position size.** Each trade is still sized so that hitting the stop costs about the usual risk per trade (2.5% of the balance with real money). With a stop 33% away instead of 2%, positions are **much smaller** (roughly 7-8% of the balance instead of up to 35%). Raise `TZ_LIVE_RISK_PER_TRADE` for bigger positions; a stopped trade then costs more.
* **Winners are still protected** from the normal plan's stop distance (-2% / -5% with real money): once a trade is up that much, the stop moves to break-even + fees, then trails and locks profit as before, and the moving take-profit works as before. A winner never rides back down to -33%.
* **The account limits still apply:** the daily-loss, drawdown and capital-floor stops close everything if the whole account falls too far.

What to expect: in simulated trading almost every *closed* trade was a winner, because losing trades simply stay open. They don't come back on their own, and their money is tied up meanwhile (each simulated run ended holding a few positions down 5-25%). The account value ended about flat. `TZ_WIDE_STOP=false` goes back to the normal plan's stops.

### Why trades were losing, and what changed

Every position was replayed in simulated real-money trading on Kraken (0.40% fee per side) and its result added up from buy to final sale:

* **Winners were small, losers were big.** Average winning position about **+$1.60**, average losing position about **-$2.00** to **-$2.80**, so even winning ~45% of positions lost money.
* **Holding losers made it worse.** The biggest losses all came from *patient exits*: holding a losing trade for a comeback, then cutting it at -4% to -5% instead of -2%. That's a weaker stop-loss, not a fix.
* **Fees were bigger than the losses.** Winners were sold in small pieces (a quarter at +1.2%, "change of mind" exits at tiny profits, small skims), and each sale paid ~0.4%. In one run, $42 of fees against a $14 total loss.
* **Winners were shaken out early.** The stop jumped to break-even after only +0.7%, so ordinary wiggles closed winners near break-even.

So the fix is the opposite of removing the stop-loss: **cut losers quickly, let winners run, sell less often.**

| | Before | Now (real money) |
| --- | --- | --- |
| Stop-loss | held losers to -5% / -12% (patient exits) | **-2%** bigger coins, **-5%** meme coins (patient exits off: `TZ_PATIENT_EXITS`) |
| Stop moves to break-even + fees | at +0.35R (about +0.7%) | at **+1R** (`TZ_FLIP_BREAKEVEN_R`) |
| Sell a quarter early at +1.2% | yes | **off** (`TZ_FLIP_BANK_FRACTION`) |
| "Change of mind" exit of a winner | any profit | only once it's made **2x the fees** (`TZ_STRATEGY_EXIT_MIN_COST`) |
| A trade's target must pay | 2x the fees | **3x** (`TZ_FLIP_MIN_EDGE`) |
| Skims (small +1.5% targets) | 4 at once | **off** (`TZ_FLIP_MAX_SKIMS`) |

On the same simulated markets this cut the losses roughly in half. The simulated prices are random, so no setting makes money there and what's left is mostly fees; on real markets the result depends on whether the entries have an edge. Tested and rejected: a wider -3% stop and a tighter -1.5% stop (both worse than -2%). The stop-loss stays: without it, a single bad trade has no floor.

### Patient exits (real money, off by default)

With real money, a losing trade isn't cut at the first dip. Each trade gets a wider **safety stop** (**-5%** for bigger coins and stocks, **-12%** for meme coins; `TZ_PATIENT_STOP_PCT`, `TZ_PATIENT_MEME_STOP_PCT`), and once it's down past where the usual tight stop would be (-2% / -5%), the Portfolio Manager checks it at every bar close:

| What it sees | What it does |
| --- | --- |
| The trade comes back to **+0.3% after fees** | closes it: a loss turned into a small win (`TZ_PATIENT_RESCUE_PCT`) |
| Momentum strongly against it (`TZ_NO_RETURN_MOMENTUM`, -0.5) **and** the move is on **1.5×** the usual volume (`TZ_NO_RETURN_VOLUME`) | no comeback in sight: cuts it early |
| Momentum against it but on light volume, or heavy volume without the momentum | holds: waits for a comeback |
| Still down after **48 h** with momentum not turning up (`TZ_PATIENT_MAX_HOURS`) | lets it go |
| The safety stop is hit | sells, always |

Volume is each bar's traded amount compared with the average of the last 20 bars. With Kraken prices it comes from Kraken's running daily total; if a feed doesn't report volume, a break below the recent swing low (above the recent high for a short) stands in for "heavy volume". Because the safety stop is wider, each real-money trade risks more (**2.5%** of the balance on Normal, **4%** on Aggressive) and the daily, drawdown and floor limits are wider to match (5% / 15% / 15% on Normal). Profit-taking (quick flips, banking slices, runners) works as before. Skims keep their own tight stop and time limit. Practice keeps the usual tight stops. `TZ_PATIENT_EXITS=false` turns this off.

### Growth ladder: smaller coins first

A small balance can't spread across every coin: Kraken's minimum orders and the fees would leave each trade too small to matter. So the bots **focus on smaller coins** (meme coins and small altcoins, which move more in percentage terms) until the balance is built up, then add bigger coins:

| Balance | Coins traded | Trades at once |
| --- | --- | --- |
| under **$250** (`TZ_FOCUS_SMALL_UNTIL`) | smaller coins only: meme coins (DOGE, SHIB, PEPE, BONK, WIF, FLOKI, ...) and small altcoins (ARB, OP, SUI, INJ, FET, ...) | 4 |
| **$250 - $1,000** (`TZ_FOCUS_MID_UNTIL`) | + mid-size coins (SOL, XRP, ADA, AVAX, LINK, DOT, LTC, ...) | 6 |
| **$1,000+** | every coin, including BTC and ETH | normal limit |

Open trades are never closed because of the ladder; it only decides which *new* trades are taken. With Kraken prices, up to 8 small altcoins that Kraken lists and that trade at least $1M a day are added automatically (`TZ_MAX_SMALL_COINS`). The goal card on Home shows the current stage. `TZ_FOCUS_SMALL_COINS=false` trades every coin from the start. Smaller coins swing harder in both directions: meme coins still trade smaller and with wider stops, and every "can it be sold?" check still applies.

### Aggressive risk (real money, opt-in)

For faster growth with real money, switch **Settings → Your goal → Real-money risk** to **⚡ Aggressive** (type RISK to confirm; `TZ_RISK_PROFILE=aggressive` on the server does the same). Practice money is never affected.

| | Normal | Aggressive |
| --- | --- | --- |
| Risked per trade (to its stop) | 2.5% | **4%** |
| Biggest single position | 35% | **50%** |
| All open stops together | 10% | **20%** |
| Open trades / meme coins at once | 6 / 3 | **8 / 4** |
| Conviction needed | 0.22 | **0.19** |
| Stop for the day after a loss of | 5% | **10%** |
| Stop everything after a drop of | 15% | **30%** |
| Stop if below what you put in by | 15% | **35%** |
| Day lock | up 2%, half given back | **up 4%, 60% given back** |

Positions are about **1.4–1.6×** bigger than Normal (the position cap is often what binds), so good runs grow faster and bad runs cost more. It doesn't make the bots better at picking trades. Some things don't change: a stop-loss on every trade, the losing-day brake, fees and "never buy what can't be sold" checks, and **no borrowing / leverage** (Kraken is used spot-only).

### Shorting (only where it really works)

The bots can also **short**: sell first and buy back lower, profiting when a price falls. Shorts follow the same guidelines as buys, mirrored: the same stop-loss and target percentages (the stop above the price, the target below, after fees), the same quick-flip plan, profit-taking, fee and transfer-fee rules, day lock and losing-day brake, and the same team vote, with a bearish majority and nobody objecting. Good news on a coin blocks shorting it, just as bad news blocks buying.

Shorting switches itself on **only where it's possible** (`TZ_SHORTING=auto`; `off` disables it):

| | Shorts? |
| --- | --- |
| Stocks on an **Alpaca margin account** (shorting enabled, $2,000+ equity) | ✅ only stocks Alpaca says are **easy to borrow** (checked every 10 min), whole shares only |
| Crypto, anywhere | ❌ Kraken is used spot-only (no borrowing), and Alpaca and Uphold can't short crypto |
| Meme coins | ❌ never: they can jump many times over, so a short's loss has no ceiling |
| Practice | follows your linked account; with none linked, practice shorts stocks the way an Alpaca margin account would |

A short never borrows to make the bet bigger: the same amount of cash is set aside as for a buy, and the cash a short sale brings in is shown as owed, so it can't be spent or sent. With the default crypto setup (Coinbase or Kraken prices), the bots therefore never short; add stocks (`TZ_SYMBOLS`) on an Alpaca margin account to use it.

### Quick flips (real money)

With real money the bots are more active and bank wins sooner (`TZ_LIVE_QUICK_FLIPS=true`, the default; practice keeps the calmer plan unless you turn it off there too):

| | Practice | Real money (quick flips) |
|---|---|---|
| Conviction needed for a trade | 0.35 | 0.22 |
| A coin whose trend still holds can be traded again | after the trend flips | 5 bars after its last trade (`TZ_REENTRY_BARS`) |
| Skims: a dip counts up to RSI | 45 | 55 (`TZ_FLIP_SKIM_RSI_MAX`) |
| Majors / stocks stop · target | -3% · +6% | -5% safety stop (cut early from -2% only if momentum and volume say so) · +4% |
| Meme coins stop · target | -8% · +15% | -12% safety stop (watch from -5%) · +10% |
| Bank part of a trade early | a quarter halfway to target | off (`TZ_FLIP_BANK_FRACTION=0`): early slices paid fees and shrank winners |
| Close a winner that's going nowhere | after 24 h (meme 6 h) | after 3 h (meme 1.5 h) |
| Skims at once · target · time limit · cooldown | 2 · +2.5% · 120 min · 15 min | off (`TZ_FLIP_MAX_SKIMS=0`): at ~0.9% round-trip fees a small skim target lost money; set 4 to turn back on |
| Target must pay the fees at least | 3× | 2× |

**Fees are counted in every trade.** Every target is profit *after* the buy and the sell fee (and slippage), each trade's result includes both fees, and the clock or a profit-taker only ever sells when the sale makes money after fees. Practice charges your real-money venue's fee: Alpaca 0.25%, Kraken 0.40% (`TZ_KRAKEN_FEE_BPS`), Uphold about 1% (`TZ_UPHOLD_FEE_BPS`). With Uphold's spread a round trip costs about 2%, so quick flips are rare there; Kraken or Alpaca suit them best.

Every trade still has a stop, and the daily loss, drawdown and capital-floor limits still stop everything (5% / 15% / 15% with real money on Normal).

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
| Someone has logged in (the owner) before going live on a public address | Nobody else on the network can flip you to live |

Paper and live keep **separate books**, so P&L, trades and the audit ledger never mix, and paper resumes where it left off. In live mode, Portfolio Manager checks positions against the broker every 30 cycles, and any mismatch pauses trading. Wallet deposits are recorded but not added to trading capital (your broker balance is the source of truth), and withdrawals are `manual_required`.

To start directly in live mode (for example, on a server after a restart), set `TZ_MODE=live` and `TZ_LIVE_CONFIRM="I UNDERSTAND THE RISKS"`. If any check fails at startup, the desk stays in paper and raises a critical alert.

## API

The dashboard signs in with an HttpOnly session cookie (a year, sliding). Scripts can instead send `Authorization: Bearer <TZ_API_TOKEN>` on every request. Tokens are never accepted in URLs.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Liveness, returns `{"ok": true}` only (no auth) |
| GET | `/api/setup/status` | `{owner, auth_required}`: has anyone logged in yet? |
| POST | `/api/signin/code` | `{"code": …}`: the 8-digit code from the logs or a 6-digit code from another device → session cookie |
| POST | `/api/devices/pair` | Make a 6-digit code (5 minutes, works once) to log in another device |
| POST | `/api/login` · `/api/logout` | `{"token": …}` (API token) → session cookie / end the session |
| GET | `/api/auth` | Signed in? |
| GET | `/api/security` | Sessions, bans, security log, limits |
| POST | `/api/security/unban` · `/api/security/signout-all` | Unblock an IP / sign out every device |
| GET | `/api/state` | Full desk state: portfolio, bots, orders, events, Executive, Grok, news, goal, today's hunt |
| GET | `/api/bots` · `/api/bots/{name}/log` | Bot team status / one bot's last 200 actions |
| POST | `/api/bots/{name}` | `{"enabled": false}` disables a bot (safety-critical bots can't be disabled with positions open) |
| POST | `/api/control/{action}` | `pause`, `resume`, `flatten`, `kill`, `reset` |
| GET | `/api/trades?mode=&symbol=&limit=&offset=` · `/api/trades.csv` | Permanent trade log with totals / as CSV |
| GET | `/api/chart?mode=&hours=` | Profit/loss over time for `paper` or `live` (after fees) with every trade on it; `hours=0` = since the start |
| GET | `/api/journal?topic=order.filled` | Raw audit trail |
| GET / POST | `/api/mode` | Preview a paper/live switch / switch: `{"mode": "live", "confirm": "LIVE"}` or `{"mode": "paper", "flatten": true}` |
| POST | `/api/goal` | `{"start": 50, "target": 10000}` |
| GET · POST · DELETE | `/api/grok` · `/api/grok/config` | Grok status / save key, model and mode / remove the saved key |
| POST | `/api/grok/ping` | Test the Grok connection |
| GET / POST | `/api/wallets` | List wallets and transfers / link a wallet `{chain, address, tag?, label?, no_tag_confirmed?}` |
| GET | `/api/wallets/check?address=` | Which network(s) an address is valid on, checksum-verified |
| POST | `/api/wallets/{id}/received` | Confirm a test send arrived (unlocks full amounts) |
| DELETE | `/api/wallets/{id}` | Unlink a wallet |
| GET | `/api/deposits/info` | Deposit address and deposit tag for each wallet |
| POST | `/api/deposits` · `/api/deposits/{id}/credit` | Record an incoming deposit / attribute a held one |
| POST | `/api/withdrawals` | Withdraw to a linked wallet `{wallet_id, asset, amount}` |
| WS | `/ws` | Live stream: `snapshot` on connect, then `action` (bot thoughts) and `event` (team events) as they happen, and `state` every cycle. Authorised by the session cookie; scripts send `{"type": "auth", "token": …}` as their first message. |

## Project layout

```
tradezone/
  core/        event bus, models, indicators, portfolio ledger
  bots/        the 12-bot team (incl. the news Reporter, Grok AI analyst, the Executive head trader, the Skimmer and the learning Coach)
  wallets.py   wallet linking, deposit/withdrawal tags, funding ledger
  market/      feeds: simulated, Coinbase (public), Alpaca
  brokers/     paper broker, Uphold broker, Alpaca broker
  desk.py      orchestrator: cycle loop, controls, kill switch
  server.py    FastAPI REST + WebSocket
  web/         single-file real-time dashboard
tests/         unit, integration, adapter (mocked HTTP) and API tests
```

## Extending

* **New strategy:** add a scoring function to `STRATEGIES` in `bots/quant.py` and weight it in `WEIGHTS`.
* **New bot:** subclass `bots.base.Bot`, subscribe to topics in `subscriptions()`, optionally implement `on_cycle()`, and add it to `TEAM`.
* **New broker or feed:** implement `brokers.base.Broker` or `market.base.MarketFeed`, then register it in `desk.build_broker` / `desk.build_feed`.
