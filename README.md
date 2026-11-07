# TradeZone

A multi-bot trading platform. Thirty-five specialised bots, including an AI analyst powered by xAI's Grok and a Coach that learns from every trade, each with its own role, work as a desk. They talk over an async event bus, and you supervise them from a real-time web dashboard with pause, flatten and kill-switch controls.

It trades **mainly crypto**, including quick flips on trusted meme coins. It runs in **paper mode by default**, trading **live market prices** with simulated fills and no money at risk. The bots learn on the same data they'll later trade live. Live trading through **Uphold** or Alpaca is supported, but you have to opt in explicitly (see [Live trading](#live-trading)).

## The bot team

| Bot | Role | What it does |
| --- | --- | --- |
| **Scout** | Market data and signal prep | Ingests every tick, rejects bad data, detects shock moves and stale feeds, and computes EMA, RSI, volatility, ATR and Donchian channels |
| **Strategist** | Market regime analysis | Classifies each symbol as *trending*, *ranging* or *volatile* (Kaufman efficiency ratio and volatility ratio, with hysteresis) |
| **Quant** | Strategy and signal generation | Runs up to ten strategies (momentum, buying dips, breakouts, MACD and on-balance volume on; VWAP, trend, rate of change, Bollinger and Stochastic available) in three voting families, weights them by the Strategist's regime call, and only proposes a buy when the team reaches consensus. It also fires the meme breakout snipes and volume snipes |
| **Grok** | AI analyst (xAI Grok) | Sends every proposed new position, with indicators, regime, recent prices and portfolio context, to Grok for an independent second opinion. In `veto` mode, trades Grok doesn't approve are never placed |
| **Risk Manager** | Risk control and position sizing | Gates every signal: kill switch, pause, exposure, concentration, max positions, cooldowns, order-rate limit. Sizes positions by volatility (risk to stop) and halves size in volatile regimes. Trips the kill switch on daily-loss or drawdown limits |
| **Executive** ★ | Head trader: final decisions | Takes the final real-time decision on every trade. Nothing reaches the broker without its authorisation. Checks team health (live prices, safety bots on duty, books balanced), re-checks the live price so it never chases a move that already happened, caps correlated crypto bets, and switches to a defensive stance (half size, higher conviction) after a losing streak or deep drawdown. Exits are always authorised instantly. Decision time is measured and shown, typically well under 1 ms. |
| **Executor** | Order execution and routing | Runs pre-trade checks, sends orders to the broker, blocks duplicates, enforces reduce-only exits, verifies every fill, and cancels stale orders |
| **Portfolio Manager** | Positions, P&L and exits | Books fills (average-cost accounting), marks positions to market, manages stop-loss, take-profit, trailing stops and profit protection, and reconciles against the broker |
| **Sentinel** | Audit, compliance and performance | Journals every decision to SQLite, keeps an independent audit ledger that is checked against the books every cycle, computes Sharpe, drawdown, win rate and profit factor, and raises alerts when any bot errors |
| **Reporter** 📰 | World and market news | Reads crypto, world and business news and the Fear & Greed index; bad news on a coin blocks buying it, scary world news makes trades smaller (see [News](#news-what-the-world-is-saying)) |
| **Skimmer** 🏄 | Profit taking and day-trade skims | Takes profits of every size (see [The Skimmer](#the-skimmer-profits-big-and-small)) |
| **Coach** 🎓 | Learning and team rules | Learns from every closed trade which strategies, coins and market moods work, and nudges the team within safe limits (see [Team rules and learning](#team-rules-and-learning)) |
| **Trend Scout** 🔥 | Discovery | Meme coins and pump.fun coins over $50k market cap with plenty of volume, and trending coins, that your exchange lists (same coin checked by price) - on every account (Kraken, Coinbase/Uphold, Alpaca); and on Alpaca the day's busiest established stocks |
| **Pullback Hunter** 🎣 | Entries | Buys the retest of a breakout |
| **Flipper** ⏱️ | Flips and timing | Volume flips, and learns the best and worst hours to trade |
| **Dip Buyer** 🔁 | Held losers | While never-sell holds a losing trade, buys more on a further dip that turns up, so it gets back to profit sooner |
| **Stacker** 🪙 | Hold stack | Slowly buys SOL, XRP, DOGE and ETH on the side; never sold by the bots; coins can be moved to and from trading by hand |
| **10 advisors** | Second opinions | Market Weather, Volatility Sizer, Momentum Ranker, Overextension Guard, Resistance Mapper, Correlation Officer, Whale Watcher, Liquidity Guard, Flash-Crash Guard, Big-Picture Trend: each can veto, nudge or resize a new trade |
| **Operations** | Upkeep | Fee Auditor (the fee really paid), Slippage Tracker, Exchange Health, Accountant (daily report), Speed Monitor (cycle times), Housekeeper (keeps the records lean) |
| **Target Learner** 🎯 · **Trade Reviewer** 📋 | Learning | Raise each coin's target to where its winners really run; size each trade finder by its record |

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
| 🪙 **Stack** | Your hold stack on its own tab: worth now, paid, change, each coin's share (a colour bar), a card per coin (amount, price now, paid each, change, buys, share vs its aim), recent purchases and moves, the next purchase, pause, and moving coins to / from the trading |
| 📈 **Trades** | The live profit & loss chart, **every trade as one card** (buy to sale, with totals and filters), and the full trade log (filter, export) |
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

**🏛️ Olympus (gods and goddesses), light, dark or 🔥 heater** (the round button in the header, or Settings → Your goal → Look; each device remembers its choice). **Olympus** is the default: ivory marble with faint veins and soft light from above, gold and deep Aegean blue, the balance on a lapis tablet framed in gold with a Greek-key band along its edges, carved-stone serif titles (from the device's own serif fonts - nothing loads from outside), gilded buttons, and a **⚡ GODLIKE!** burst for a winning trade; every device switched to it once, and any other look can be picked again. **heater** is a dark, fiery look - fire gradients, a glowing balance, flames along the bottom of the screen and embers rising (a winning trade says **ON FIRE!**). It's only a look: nothing about trading changes. **Dark** is the same clean design at night. **Easy to read on any screen:** every visible text is checked - in all four looks, on a phone and a computer - for contrast against its real background (at least 4.5:1, 3:1 for large text, the accessibility standard), size (12px or more) and, on a phone, places to tap (40px or more). The **clean & vibrant** light theme is light and airy - white cards on a soft background, crisp type (Inter, bundled with the app, SIL Open Font License, so nothing loads from outside and the security policy stays strict) and generous spacing. The colour goes where it means something: the balance sits on a bold gradient (cool violet-to-cyan on practice money, warm pink-to-orange on real money, so the two can't be confused), buttons are bright, and gains and losses are a clear green and red on white. The charts take their colours from the theme. A **✨ star power** readout shows your balance, with a meter for today's move and a rank that grows with your total return (Cadet → Pilot → Captain → Commander → Admiral); your win streak lights up as stars, and winning trades end with a **SUPERNOVA!** burst. Animations switch off if your phone has "Reduce Motion" on.

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

* **Bot floor:** the 35 bots in the order a trade flows through them (Scout → Trend Scout → Reporter → the 10 advisors → Strategist → Quant → Grok → Risk Manager → **Executive** → Executor → Portfolio Manager → Skimmer → Flipper → Dip Buyer → Stacker → Speed Monitor → Housekeeper → Sentinel → Coach → Target Learner → Trade Reviewer). The header shows the Executive's stance and decision speed. Each tile shows what the bot is doing right now, how busy it is (actions/min, total), when it last acted, and **lights up in its colour** whenever it acts. Tap a tile (or a bot card) for that bot's full log of its last 200 actions, which keeps updating while open.
* **Live activity:** every bot "thought" (e.g. *no consensus to buy AAPL: momentum against*) and every team event (signals with their votes, Grok verdicts, Risk approvals and rejections with the reason, orders, fills, stop moves, alerts). Filter by Trades, Signals, Risk, Alerts, Bot thoughts or any single bot.
* **Everything in your exchange account (Money):** every coin a linked Kraken account holds, in full, with its value - split into the bots' open trades, the hold stack, and **yours** (coins held before linking or added since, which the bots never trade or sell and which used to show nowhere), plus coins staked or in earn programs (Kraken's ".S", ".M", ".B", ".P" balances; listed, never traded; auto-earn ".F" counts as normal coins). Read-only: one balance read, at most every 30 seconds. Other exchanges say plainly that they can't list this yet.
* **Trade pop-ups:** the moment a trade is taken or closed, a card pops up at the top of the screen (on any page): a buy shows the coin, how much at what price, why in everyday words, and its stop and target; a sale shows the result after both fees in $ and %, what it sold for and was bought at, how long it was held, and why. Green for a profit, red for a loss. Tap it for the Open or Trades tab, ✕ to dismiss; each goes after 8 seconds (it waits while the mouse is over it), at most 2 at once on a phone so the balance stays in view, 4 on a computer. Coins moved by hand to or from the hold stack don't pop up (they aren't trades). Switch them off in Settings → Trade pop-ups (remembered on that device).
* **Your trades:** every trade as one card, from buy to sale, built from its fills: what was bought and at what average price, what it sold for (and how the price moved), the result after **all** fees in $ and %, how long it was held, how many buys (Dip Buyer adds) and in how many parts it was sold, the fees, and why it was bought and why it was sold in everyday words ("Breakout: the price broke out of its range", "Safety stop reached"). Tap a card for each fill. Open trades show where they stand now (after fees); a trade moved into the hold stack says so (that isn't a sale). At the top: trades closed, win rate, the result after fees, average win and loss, best and worst, average time held. Filter by wins / losses / open, today / 7 days / 30 days / all, practice / real money, and account.
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
* **Safer sizing for meme coins:** they risk half of what a normal trade risks (`TZ_MEME_RISK_MULT=0.5`), and at most 5 are held at once (`TZ_MAX_MEME_POSITIONS`; 6 on Aggressive, 7 on Bold).
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

**On every account.** It used to need Kraken's prices; now it knows what each account's exchange lists - Kraken's pairs, Coinbase's products (Uphold accounts also check Uphold lists the coin), Alpaca's coins - so every linked account finds new coins for its own team. The same-coin price check runs on all three.

**Stocks, on Alpaca accounts.** Every half hour it reads Alpaca's free screener (the day's most-traded stocks and biggest risers) and adds up to 10 established ones: a share price of $10 or more, at least $50M traded (today or yesterday - early in the morning hardly anything has traded yet), listed on a main US exchange, fractional shares allowed, and never a leveraged or inverse fund. Its price history is loaded so the bots can trade it at once; big risers get the same small conviction boost as trending coins; a stock that stops qualifying is dropped, never while held. Stocks join from a $1,000 balance (the growth ladder). **Not tested on price history** (no history of which stocks were busy on which day was available here): it only picks big, liquid names, and every trade still goes through all the usual checks and limits.

**News for stocks and new coins.** On Alpaca accounts the Reporter also reads Alpaca's news, where each headline is tagged with the tickers it's about, so a recall, a probe or an earnings beat counts for that stock. Coins the Trend Scout found are matched in the other feeds by their $TICKER or full name. Company words were added to the scoring (beats / misses estimates, guidance raised / cut, recalls, probes, upgrades / downgrades).

The 14th bot plugs the team into the coins where the action is. Every **30 minutes** (`TZ_DISCOVERY_EVERY_MIN`) it reads CoinGecko's free public market data (no account needed):

* **meme coins** with a market cap of at least **$50k** (`TZ_DISCOVERY_MIN_MCAP`) and **promising volume**: at least **$100k** traded in 24 h (`TZ_DISCOVERY_MIN_VOLUME`) and at least **5%** of the market cap changing hands a day (`TZ_DISCOVERY_MIN_TURNOVER`), so dead coins are skipped;
* the coins **trending** on CoinGecko right now.

Every one of them that the exchange lists against USD (with Kraken prices) is added to what the bots watch and trade, trending coins first, up to 100 (`TZ_MAX_DISCOVERED`; was 20), with recent price history loaded so the bots can act at once (one request about a second apart, in the background, to stay inside the exchange's rate limit). Each still needs its listing, market value and trading volume to qualify, so the number found depends on the day - and many are small, jumpy coins (meme rules: smaller bets, wider stops). Not tested on price history as a change. Meme coins get the meme rules (smaller bets, wider stops), trending coins a small conviction boost (`TZ_TRENDING_NUDGE`). Coins that stop qualifying are dropped again, never while the bots hold one. **Today's hunt** on Home lists what's trending. `TZ_DISCOVER_COINS=false` turns it off.

Two limits keep this safe: a coin is only **bought** if the "never buy what can't be sold" checks pass (listed for buying and selling, a tight bid/ask gap, enough daily trading for the position), so a thin coin is watched but not bought. And coins that **only trade on decentralised exchanges** (most $50k-$1M meme coins) can't be bought: TradeZone trades through the exchange account, and DEX-only tokens carry a high risk of tokens that can never be sold.

### 🏦 Several real-money accounts at the same time

Besides the main account (Settings → Linked accounts), you can link up to **5 more** on the Money page (**Your accounts → + Link another account**): Kraken, Alpaca or Uphold, in any mix, including two accounts at the same exchange.

- **Each account gets its own complete bot team**, with its own book, risk limits, daily-loss and drawdown stops, kill switch and practice/LIVE switch. All the teams trade at the same time, and each one trades only its own balance.
- **Balances are shown separately.** Each card shows what the bots count (balance, today's profit or loss, cash, open trades) next to the exchange's own figure, which is re-read once a minute. The header adds up the accounts on real money.
- **A loss limit or a stop in one account never touches another.** **Stop all accounts** closes every account's trades, the main one included, and halts them all at once.
- **The same safety checks apply to every account.** A Kraken key that can withdraw is refused, Alpaca paper keys are refused, and every account starts on practice money. Switching an account to real money means typing LIVE on its own card.
- **The same account can't be linked twice.** Two teams trading one balance would trip over each other, so a key that is already in use is refused. For two teams at one exchange, use two accounts or sub-accounts.
- Each account keeps its keys and records in its own database file next to the main one (`tradezone-account-<id>.db`). The main database only lists which accounts exist. Everything comes back after a restart, and an account that was live resumes live only after its exchange account checks out.

**TastyFx isn't linkable yet.** Its trading API is not a simple web API like Kraken's or Alpaca's: it is either tastyfx's own WebSocket protocol or FIX 5.0. Its documentation couldn't be reached from the development environment, and guessing at a real-money order protocol isn't safe. Forex with TastyFx would also need its own price feed, lot sizes and weekend market hours.

### 💰 The make-me-money protocol

No setting can promise profit. The **Make-me-money protocol** card on the home screen is the path that loses least while you find out whether the bots make money on real markets. Each step shows its live status:

1. **Practise first.** How many days practice has run and what it made; a week or two shows how the bots do on today's market.
2. **Link your exchange.** Trading only; the bots can never withdraw.
3. **Start real money small.** Set a **real-money cap** right on the card (e.g. $50, remembered; `TZ_LIVE_MAX_CAPITAL`). Add more only after a few good weeks. A new cap takes effect the next time real money is switched on.
4. **Use the tested-best settings.** It lists any bot setting that has drifted from the combination that did best together on real price history. **Use the tested-best settings** puts them all back in one tap, on every linked account. Your risk level and cap stay yours, and settings fixed on the server are named.
5. **Review each week.** The last 7 days on real money: trades won, profit banked, and any losing trades being held.
6. **Take profits out now and then.** Profit is only really yours once it's out of the exchange. The bots never withdraw; you do.

### 🧠 Learning bots

The team already learned three ways: the **Coach** (which strategies and coins work, how high the bar is), the **Flipper** (the best and worst hours) and the **Fee Auditor** (the fee really paid). Two more learn from every finished trade and remember across restarts:

- **🎯 Target Learner**: records how far each coin's *winning* trades ran. After 8 winners in a coin, it raises that coin's take-profit to where 60% of them reached, up to 1.6× the usual target, so winners in coins that run further aren't sold too soon. It never lowers a target (`TZ_LEARN_TARGETS_RAISE_ONLY`). Letting it lower them sold winners too soon in testing: +$580 / +$423 vs +$668 / +$430.
- **📋 Trade Reviewer**: keeps a record for each way the team finds trades: strategy signals, breakout snipes, volume snipes, pullbacks, flips, dip buys and skims. After 6 finished trades, a finder that keeps winning sizes its next trades up (to 1.25×), and one that keeps losing sizes them down (to 0.5×). One that's losing money overall is never sized up.

Both show what they learned on the **What the team has learned** card (`TZ_LEARN_TARGETS`, `TZ_LEARN_SOURCES`).

Tested on real price history with longer runs, so there's something to learn from (10 runs of $500 per half): no new learners +$668 / +$430; Trade Reviewer +$668 / +$434; raise-only Target Learner unchanged (winning runs rarely went past the usual targets in that history). They're small effects now. Learners earn their keep over months of real trades, not in a test of a few days.

### 🔁 The Dip Buyer: cycling held losers back to profit

While losing trades are held (**never sell at a loss**), the Dip Buyer buys more of one once it has dipped further and starts to turn up, so its average price comes down and it gets back to profit sooner. The comeback sale then takes it out once it beats its fees.

It buys when all of these hold:
- the coin is **6% or more below its average price** (meme coins 12%, `TZ_DIP_STEP_PCT`);
- the price turns up on this bar, with an oversold RSI (15-45);
- no advisor objects (a flash crash, Bitcoin falling hard, a dead market);
- trading isn't paused or stopped for the day.

It buys **half as much again** (`TZ_DIP_ADD_FRACTION`), **at most twice per trade** (`TZ_DIP_MAX_ADDS`), never past the normal biggest position, and may use all the cash (`TZ_DIP_KEEP_CASH`, 0; it used to keep a quarter back - you asked for all the cash to go to crypto). It only runs while never-sell is on (otherwise losing trades are cut). `TZ_DIP_BUY=false` turns it off.

Tested (runs of $500): on real price history it rarely triggered: no change on the first half, +$12 on the second (+$509 vs +$496). On random prices, where coins keep falling, it made the worst run deeper (-$97 vs -$78; total $2,855 vs $2,865). Buying more of a falling coin lowers the price you need to get back to, but puts more money into the coin. If the coin keeps falling, the loss grows.

### ⚙️ Processing helpers: the Speed Monitor and the Housekeeper

- **⏱️ Speed Monitor** times every trading cycle and how long each bot spends working, including waiting on the exchange. Its tile on the bot floor shows the last and average cycle time, how much of the time between cycles the team uses, and the three busiest bots each minute. If cycles keep taking more than half the time between them, it warns that the bots are falling behind and names the busiest.
- **🧹 Housekeeper** keeps the records lean. Every event the bots publish is recorded, and that added about 40 MB a day (over 1 GB a month), enough to fill a small hosted disk within weeks. Every hour it clears:
  - old working notes: trade ideas, turn-downs and market-mood updates after 2 days (`TZ_JOURNAL_KEEP_DAYS`);
  - order updates and alerts after 30 days;
  - chart points older than 2 days, thinned to one every 10 minutes (the "All" chart still covers everything).

  **Trades, hold-stack buys, deposits, withdrawals, wallet changes, your own control actions and balance corrections are kept forever.** It compacts the database once a day to give the space back to the disk.

The cycle itself got faster too, from 7.1 ms to 4.2 ms with 20 coins. The Flash-Crash Guard's volatility maths used exact fractions (a third of all the time), and the coin-size ladder was worked out about 30 times a cycle instead of once.

### 🛑 Never sell at a loss

**On by default** (`TZ_NEVER_SELL_AT_LOSS`; **Settings → Your goal → Never sell at a loss**). A trade below break-even after fees is **held until it recovers**:
- no stop-loss, early cut or bot ever sells it, and the Risk Manager turns down any sale at a loss as a safety net;
- the automatic daily-loss, drawdown and floor limits **stop new trades**, but don't sell open ones;
- **your own Close all trades and Emergency stop still sell** (you chose to);
- meme coins trade at half the usual biggest size (`TZ_NEVER_SELL_MEME_SIZE`), because held without a cut they can fall a long way.

The Settings row lists the losing trades being held and how far down each is.

Tested both ways (runs of $500, real-money Kraken fees):

| | Real price history (20 runs, first / second half) | Random prices (6 runs) | Worst random run |
| --- | --- | --- | --- |
| Cutting losers (stops + early cuts) | +$398 / +$295 | $2,848 | -$36 |
| **Never selling at a loss** | **+$537 / +$496**, every closed trade a win | $2,865 | **-$78** |

Re-checked after a flaw in the replay tool was fixed: the earlier "20 runs" re-used only about 5 distinct mixes of history. On 20 genuinely distinct runs per half: cutting losers +$515 / +$152, never selling at a loss +$582 / +$401. Real prices (the S&P 500, Google, EUR/USD) tend to come back after a dip, so holding paid off. Random prices don't, and there the losses didn't disappear: they sat in unsold trades. Coins were still down 9-56% at the end of several runs, and the money in them couldn't trade. **"Every closed trade a win" doesn't mean nothing was lost.** A coin that never recovers, like many meme coins, keeps its loss and ties up the money. The balance shown always counts these held trades at today's price. Turn it off to cut losing trades again.

### 🚀 pump.fun coins

The Trend Scout watches **coins launched on pump.fun** (CoinGecko's pump.fun category, free public data, no account) with the same bar as every meme coin:
- popular: **over $50k market cap** (`TZ_DISCOVERY_MIN_MCAP`);
- **plenty of volume**: at least $100k traded a day, and at least 5% of the market cap changing hands.

The busiest that **your exchange lists** are plugged into the watchlist with their price history, so the volume snipes and breakout snipes can act on big moves at once. They get the meme-coin rules: smaller bets, wider stops, the early cut when momentum and volume turn, and no more than 5 meme coins at once. The home screen shows which ones came from pump.fun. `TZ_DISCOVER_PUMP=false` turns it off.

**There's no pump.fun wallet link, on purpose.** pump.fun has no trade-only API key. Trading there directly needs a Solana wallet's private key, and that key can also send every coin in the wallet anywhere, which breaks the rule that the bots can never withdraw. Most pump.fun coins also collapse or can't be sold again. Buying them only once an exchange lists them keeps the rules: listed for buying and selling, a real bid/ask market, and no withdrawals.

**Copycats are skipped.** pump.fun is full of coins that reuse a famous ticker (a "PEPE" that isn't PEPE). A discovered coin is only plugged in when its CoinGecko price matches your exchange's price for that ticker within 5% (`TZ_SAME_COIN_TOLERANCE`). This now applies to every discovered coin, not only pump.fun ones. A pump.fun coin whose price can't be checked is skipped.

"Always sell before the price drops" isn't something any bot can promise: no one knows the next move. What the bots do is cut a meme coin early when momentum turns down on heavy volume, lock in profit once a trade is up, and stop for the day after a bad one.

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

So every *completed* round ends with more coins. Not every round completes, though: when a coin keeps rising, the slice was sold early (as profit). The **What the team has learned** card shows the coins gained and any slice waiting to be bought back. **It is off by default now** (`TZ_ACCUMULATE=true` turns it on): on real price history it cost money (see *Tested on real price history* below).

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

How the bots run today, with real money, and the switch for each. Every "off" was tested and made less on real price history; the sections below give the numbers. **Settings → Make-me-money protocol → Use the tested-best settings** puts all of these back in one tap.

| Feature | Real money | Switch |
| --- | --- | --- |
| **Never sell at a loss** (losing trades held until they recover; your Close all / Emergency stop still sell) - **except at the loss floor** | **on** | Settings → Your goal, `TZ_NEVER_SELL_AT_LOSS` |
| **Loss floor**: a trade 20% below what was paid is sold, even with never-sell on (the most a trade can lose) | **on (20%)** | Settings → Your goal, `TZ_MAX_LOSS_PCT` |
| Dip Buyer (buys more of a held loser on a further dip, at most twice) | on (while never-sell is on) | `TZ_DIP_BUY` |
| **Underwater cap** (no new buys while trades not yet back above break-even tie up 15% of the balance) | **on** | Settings → Your goal, `TZ_LOSER_CAP` |
| **Falling-market guard** (no new buys of a coin while it's below its 4-hour average and that average is falling; selling, stops and your own buys never blocked) | off (optional) | Settings → Your goal, `TZ_FALLING_GUARD` (`coin` / `market` / `off`) |
| Stop-loss 20% from the buy price (was 33%) | on; with never-sell on it sells a loser only at the loss floor | `TZ_WIDE_STOP`, `TZ_WIDE_STOP_PCT` |
| Patient exits (cut early when momentum and volume say no comeback) | on, but with never-sell on they don't sell at a loss | `TZ_PATIENT_EXITS` |
| Whole cash balance trades (sized for the early cut, not the -33% stop) | on | `TZ_FULL_CASH` |
| Every profit sale keeps more than its fees (and comebacks) | on | `TZ_PROFIT_OVER_FEES`, `TZ_FEE_RULE_RESCUE` |
| Quick flips (closer targets after fees) | on | `TZ_LIVE_QUICK_FLIPS` |
| Moving take-profit past +10% | on | `TZ_TP_STEP` |
| Strategies: momentum, buying dips, breakouts, **MACD**, **on-balance volume** | on (5 of 10; the other 5 tested worse) | Settings → Trading strategies, `TZ_STRATEGIES` |
| Meme breakout snipes, volume snipes (4× volume), Pullback Hunter, Flipper | on | Settings → Trading strategies |
| Trend Scout: meme coins and **pump.fun** coins over $50k with plenty of volume, trending coins, on every account | on (only coins your exchange lists, same coin checked) | `TZ_DISCOVER_COINS`, `TZ_DISCOVER_PUMP` |
| Stock discovery (Alpaca): the day's busiest stocks, $10+, $50M+ traded, main exchanges, no leveraged/inverse funds | on (not tested on price history - see below) | `TZ_DISCOVER_STOCKS`, `TZ_MAX_DISCOVERED_STOCKS` |
| News per stock and coin from Alpaca's news service | on (Alpaca accounts) | `TZ_NEWS_ALPACA` |
| Up to 100 trades at once, up to 5 meme coins | on | `TZ_MAX_OPEN_POSITIONS`, `TZ_MAX_MEME_POSITIONS` |
| Hold stack (SOL, XRP, DOGE, ETH and Onyxcoin XCN bought slowly on the side, never sold) | on | `TZ_STACK` |
| Learning: Coach, Flipper hours, Fee Auditor, Target Learner, Trade Reviewer | on | `TZ_COACH_LEARNING`, `TZ_LEARN_TARGETS`, `TZ_LEARN_SOURCES` |
| Speed Monitor, Housekeeper (records kept lean) | on | `TZ_JOURNAL_KEEP_DAYS` |
| Reinvesting today's profit | on (a quarter) | `TZ_REINVEST_SHARE` |
| Growth ladder (smaller coins first on small balances) | on | `TZ_FOCUS_SMALL_COINS` |
| Daily goal ($1,000, a floor not a ceiling) | on | `TZ_DAILY_GOAL_USD` |
| "Never buy what can't be sold" checks | on | (always) |
| Shorting | **off** (tested: with never-sell on, shorts lost money) | `TZ_SHORTING` |
| Alpaca: US stocks (SPY, QQQ, AAPL, NVDA, MSFT) next to its crypto | on, long only | `TZ_ALPACA_STOCKS` |
| Day-trade rule (stocks, under $25,000: the bots use at most 2 of the 3 day trades allowed in 5 days) | on | `TZ_DAY_TRADE_GUARD` |
| Quick-flip slices, skims, accumulating, profit sales at the ask, the fee rule on locked stops | **off** (each made less) | `TZ_FLIP_BANK_FRACTION`, `TZ_FLIP_MAX_SKIMS`, `TZ_ACCUMULATE`, `TZ_MAKER_EXITS`, `TZ_FEE_RULE_STOPS` |
| Aggressive / Bold risk levels | opt-in | Settings → Your goal |

The full list of settings, with every default, is in `.env.example`.

### The Alpaca team: stocks next to crypto, no shorting, the day-trade rule

A linked Alpaca account runs its own bot team on **Alpaca's crypto only** - all the cash goes to crypto. (Dropping the 25% the Dip Buyer and the hold stack used to keep back changed almost nothing on real price history - +$397 / +$172 rising vs +$396 / +$173, identical falling and sideways - because the underwater cap stops new buys before those reserves come into play. Switching that cap off is what puts more money to work, and it loses about twice as much when prices fall.) Stocks can be switched back on: five big US stocks/ETFs (`TZ_ALPACA_STOCKS=SPY,QQQ,AAPL,NVDA,MSFT`: liquid, fractional, no commission) and the stock search (`TZ_DISCOVER_STOCKS=true`); stocks trade only in US market hours, crypto around the clock. Under a $1,000 balance the growth ladder keeps the team on smaller coins (stocks join from $1,000).

Tested like everything else, on real price history in rising, falling and sideways markets ($2,000 account, 8 windows each, tuning half / unseen half). Only daily stock history was available here, replayed minute by minute at normal intraday stock volatility, so treat it as a careful estimate rather than proof:

| Alpaca team | Rising | Falling | Sideways | All three |
| --- | --- | --- | --- | --- |
| Crypto only | +$907 / +$350 | −$747 / −$449 | +$8 / −$38 | +$168 / −$137 |
| Crypto + stocks, with shorting | −$229 / −$113 | −$397 / −$143 | +$5 / +$20 | −$621 / −$236 |
| **Crypto + stocks, no shorting** | +$962 / +$369 | −$832 / −$467 | +$41 / −$29 | **+$171 / −$127** |

**Shorting is now off by default.** With never-sell-at-a-loss on, a short (a bet on a fall) that goes the wrong way is held while the price keeps rising, and that loss has no ceiling. Long-only stocks added a little over crypto alone, on both halves: more when prices rise or go sideways, a bit more lost when they fall.

**The US day-trade rule.** Under $25,000, an account may make at most 3 day trades (buy and sell the same stock on the same day) in any 5 trading days; Alpaca refuses the 4th, and repeated breaches lock the account for 90 days. The bots count them (and use Alpaca's own count when it's higher) and stop at 2: after that, a stock bought today is sold from the next trading day, and the last day trade is kept for your own Close all / Emergency stop, which always go through. Crypto isn't covered by the rule.

**The overview switch.** With more than one account linked, Home has tabs above the balance: *All accounts* (the total, today's change, how many are trading, paused or stopped, every open trade, and Pause / Resume / Stop all), *Main*, and one per linked account (its balance, status, day trades used, open trades and its own buttons). The choice is remembered on that device.

### Tested in rising, falling and sideways markets: the underwater cap

Every earlier test window turned out to be a **rising** market: the real price history behind the replays (S&P 500, Google, EUR/USD) mostly went up, so the settings above had only been proven where prices recover. Falling and sideways markets were added, built from the same real moves (mirrored, so the same swings run downhill; or with the drift taken out), and the bots were re-tested with the 300 bars of history they get at start-up. 16 windows of $500 each per market, on the half used for tuning and the half kept unseen:

| | Rising | Falling | Sideways | All three |
| --- | --- | --- | --- | --- |
| Before (tuned / unseen) | +$469 / +$317 | −$743 / −$514 | −$37 / −$77 | −$311 / −$274 |
| **Underwater cap 15%** | +$396 / +$173 | −$393 / −$262 | −$29 / −$23 | **−$25 / −$112** |

With never-sell-at-a-loss on, a slide otherwise pulled almost the whole balance into losing trades that are then held all the way down (the "100% win rate" hides them: they're never closed). The cap stops new buys, and the Dip Buyer's adds, while trades more than 0.5% below break-even after fees tie up 15% of the balance; buying resumes as they recover. **The cost is real: less profit when prices only rise** (about a sixth less on the tuning half, about half on the unseen half). It's a switch in Settings → Your goal. **Why it seems to "miss" good trades:** a brand-new trade starts about 0.8% below break-even (both fees), so it counts toward the cap from its first minute, and one trade can fill it - in a rising market the cap turned down about four good ideas for every trade taken (as often good as the trades that got through). Letting new trades count only after 30 minutes to 4 hours (`TZ_LOSER_CAP_GRACE_MIN`) earned up to 44% more when prices rose - but lost 18-32% more when they fell and did worse sideways on unseen data; with the falling-market guard added it lost more still. Those "missed" trades are the price of the protection, so the wait stays at 0. Also tried and dropped: a downtrend guard (no buys while a coin or the market fell over the last 30-240 minutes - it made almost no difference: these slides are slow grinds that hours of history can't tell from noise), caps of 10-25%, and counting a trade by its price drop alone (0.5-2%). Turning never-sell off lost less in falling markets in a quick check (8 windows), but it sells at a loss, which you asked the bots not to do.

### Tested in rising, falling and sideways markets: the falling-market guard (off - optional)

No new buys of a coin while it's below its 4-hour average **and** that average is lower than an hour ago; it waits for a full 4 hours of prices before judging (the app loads 5 hours at start-up). Selling, stops and your own buys are never blocked. Replayed with the 300 bars the app loads first, 6 windows of $500 per market (tuning / unseen half):

| | Rising | Falling | Sideways |
| --- | --- | --- | --- |
| Without it | +$150 / +$41 | −$125 / −$97 | −$6 / −$17 |
| **With it** | **+$169 / +$61** | **−$124 / −$92** | **−$3 / −$10** |

A check on 16 windows per market (tuning / unseen):

| 16 windows | Rising | Falling | Sideways |
| --- | --- | --- | --- |
| Without it | +$396 / +$173 | −$389 / −$256 | −$29 / −$23 |
| With it | +$415 / +$175 | **−$371 / −$230** | −$30 / −$25 |

It lost 5-10% less when prices fell and earned a little more when they rose, but did slightly worse sideways on both halves (about $2 over 16 runs) - so it didn't beat "without it" everywhere and is **off by default**; a switch in Settings if you'd rather lose less in a fall. Even so, it's modest: in a fall it saves only a little, because the bots still buy as a slide begins (before the average turns down) and never-sell then holds those trades. Also tried: a 2-hour and an 8-hour average, and pausing all buying while half the coins fall - each lost to "without it" somewhere. Judged on less than the full 4 hours of history, it did worse sideways, which is why it waits. (An earlier guard that looked only at the last 30-240 minutes' change made no difference - see the underwater cap above.)

### Where the money goes (every market)

Replayed on real price history (16 windows of $500 per market, tuning / unseen half; trade results are after fees):

| | Total | Winning trades | Sold at a loss | Still held below cost | Fees paid |
| --- | --- | --- | --- | --- | --- |
| Rising | +$396 / +$173 | +$395 / +$197 | $0 | −$24 / −$46 | $183 / $119 |
| Sideways | −$29 / −$23 | +$48 / +$59 | $0 | −$90 / −$94 | $41 / $45 |
| Falling | −$389 / −$256 | +$14 / +$18 | −$81 / −$21 | −$328 / −$258 | $19 / $20 |

The loss is almost all **trades still held below what was paid** (never-sell holds them; about two per window, 35-45% of the money), then fees, which eat most of a sideways market's small wins. The cause is entries no better than chance (see "Better buy signals"). Stricter entries were tried - a conviction bar of 0.30 or 0.40 for real money (it's 0.22), 3 agreeing indicators instead of 2, and each with the dip-turn filter: all did better sideways on the tuning half (up to +$27 vs −$29) and all worse on the unseen half (−$28 to −$42 vs −$23) - an "improvement" that doesn't repeat on unseen data is luck - so the defaults stay.

### Losing money in falling markets: where it goes, and what was tried

In a falling market almost the whole loss is **trades still held below what was paid** (never-sell holds them; they average 12-14% down, short of the 20% loss floor): 16 windows of $500, tuning / unseen half - total −$389 / −$256, of which held trades −$328 / −$258, trades already sold −$66 / −$3, the hold stack −$4 / −$5. About 30% of the balance ends up in them. Every fix tried cut that loss, and every one cost about as much profit when prices rise, because when a trade is opened no rule here can tell the start of a rise from the start of a fall:

| 16 windows, tuning / unseen | Rising | Falling | Sideways |
| --- | --- | --- | --- |
| As it is | +$396 / +$173 | −$389 / −$256 | −$29 / −$23 |
| Biggest trade 20% (was 35%) | +$267 / +$130 | −$322 / −$219 | −$27 / −$8 |
| 1.8% risked per trade (was 2.5%) | +$345 / +$134 | −$329 / −$190 | −$15 / −$33 |
| Underwater budget 20% (`TZ_UNDERWATER_BUDGET`) | +$273 / +$135 | −$296 / −$197 | (not run) |
| Loss floor 15% (was 20%) | +$396 / +$173 | −$380 / −$249 | −$39 / −$23 |
| Loss floor 10% | +$386 / +$175 | −$369 / −$239 | −$41 / −$45 |

**Spreading wider (more, smaller trades).** On average the bots hold about 2 coins and keep 30-41% of the balance invested - one big trade fills the underwater cap. Smaller trades with a looser cap made 27-34% more trades and up to 27% more on the unseen half when prices rose, but never beat "as it is" on the tuning half, and lost more when prices fell (biggest trade 25% / cap 30%: +$391 / +$219 rising, −$481 / −$351 falling; 15% / 30%: +$274 / +$164, −$393 / −$271; 20% / 25-40%: +$325-346 / +$197-221, −$421-500 / −$288-341). More money working is more money caught in a fall.

**Waiting longer at the bid for the lower maker fee** (`TZ_MAKER_WAIT_S`, 20 s): fees are close to half the profit, so 1-10 minutes were tried (after fixing a bug that dropped any maker buy left waiting past 60 s). 3-5 minutes saved about $20 in fees and earned 1-4% more when prices rose (+$399-414 / +$175-177 vs +$396 / +$173), but lost more when they fell (−$404 vs −$389 on the tuning half): a bid left waiting fills precisely when the price is dropping through it. It stays at 20 s.

**Better buy signals.** Every buy taken was scored against buying at a random moment (did the price rise 1.5% before falling 3%?). Overall the bots' buys barely beat random; dips led by **mean reversion** (RSI under 35, the most common lead sideways) went bad *more* often than random buys in all three markets (rising 16% / 26% vs 10% / 12%; falling 54% / 59% vs 44% / 33%; sideways 30% / 31% vs 20% / 21%), while pullback and breakout buys held up. Filters for those dips (`TZ_MR_BUY_FILTER`: wait for the turn, only above the 50-bar average, or both; `TZ_MR_RANGING_WEIGHT`, its sideways share of the vote, 0.7):

| 16 windows, tuning / unseen | Rising | Falling |
| --- | --- | --- |
| As it is | +$396 / +$173 | −$389 / −$256 |
| Wait for the turn | +$414 / +$187 | −$358 / −$287 |
| Only above the 50-bar average | +$414 / +$157 | −$355 / −$247 |
| Half the vote (0.35) | +$438 / +$174 | −$371 / −$301 |

Each won one half and lost the other somewhere, so they stay off.

**Locking profit in sooner** (so a winner can't turn into a loser): break-even + fees at half or three-quarters of the usual gain (`TZ_FLIP_BREAKEVEN_R`, 1.0), keeping 70% of the best gain instead of 50% (`TZ_PROFIT_LOCK_PCT`), stops never under the fees (`TZ_FEE_RULE_STOPS`). The closest, locking at half the gain, was a little better rising (+$397 / +$182 vs +$396 / +$173) and no worse falling (−$385 / −$256 vs −$389 / −$256), but a little worse sideways (−$32 / −$24 vs −$29 / −$23: an earlier lock sells trades that would have reached their target). In these replays no trade that was in profit after fees ended as a realized loss anyway (every sale in rising and sideways markets was a win), so nothing changed.

**Taking profit faster.** A smaller real-money target for bigger coins (+3% or +2.5% after fees instead of +4%) banked wins sooner but earned less (+$378-379 / +$159-163 rising vs +$396 / +$173: the extra round trips' fees ate it); +5% earned more on the tuning half, much less on the unseen half (+$425 / +$142). Selling everything at the target: +$415 / +$172. Trading a coin again 2 bars after its last idea instead of 5 (`TZ_REENTRY_BARS`): the best rising (+$409 / +$181) but lost more falling on the unseen half (−$266 vs −$256). None won everywhere; nothing changed.

**Divide and conquer: each coin by its own trend** (`TZ_TREND_SPLIT_RISING_PAST_CAP`, `TZ_TREND_SPLIT_FALLING_SIZE`). Each coin is read by its own 4-hour average - up, down or flat. Letting rising coins past a full underwater cap earned far more when prices rose (+$445 / +$326 vs +$396 / +$173) but lost far more when they fell (−$551 / −$409 vs −$389 / −$256): even a falling market has short uptrends, and they pull money in just before the next drop. Buying falling coins at half size lost a fifth less on the tuning half when prices fell (−$308 vs −$389) but earned less on the unseen half when they rose (+$156 vs +$173). Not buying falling coins at all is the falling-market guard (same results: slightly worse sideways). Combined: +$458-466 / +$314-337 rising, −$438-558 / −$360-374 falling. Off by default.

**The trade finders, re-checked in all three markets** (they were chosen when every test window happened to be a rising market): pullback buys off was better rising (+$404 / +$193 vs +$396 / +$173) and falling (−$375 / −$253 vs −$389 / −$256) but much worse sideways on the unseen half (−$45 vs −$23); the volume-spike snipe off (or needing 6x volume) lost less falling but earned less rising on the unseen half (+$146); 3x volume, the breakout snipe off, the Flipper off and MACD/OBV off each lost somewhere. All stay as they are.

**Taking profit as soon as it covers the costs, then pickier buys.** Real-money targets of +1.5% / +1% after fees (meme coins +3% / +2%, instead of +4% / +10%), with and without a stricter buy bar (0.35, three agreeing indicators): falling markets lost two-thirds less (−$131 / −$75 vs −$389 / −$256) - but mostly because a target that small barely clears the fees, so most trades were never opened (2 trades instead of 22 in one window) - and rising markets earned 70-85% less (+$63-103 / +$47-50 vs +$396 / +$173): the winners were cut off. The Skimmer's quick +1.5% flips (`TZ_FLIP_MAX_SKIMS`) earned a little more rising (+$413 / +$182) but lost more falling (−$465 / −$285). Winners keep running: at the target a third is banked, the stop moves into profit, and the rest rides with no ceiling.

None beat "as it is" in every market, so nothing changed by default. The lower loss floors also sell three times as much at a loss. If you'd rather lose less in a fall and accept less profit in a rise, the smaller trade size or the underwater budget are the honest dials (the risk level in Settings, `TZ_LIVE_MAX_POSITION_PCT`, `TZ_UNDERWATER_BUDGET`).

### Tested on real price history: accumulating and the quick-flip slice are off

Random prices can't show whether a strategy has an edge, so the whole real-money team (Kraken fees, every bot) was replayed on **real price history**: S&P 500 daily bars 1993-2021, Google daily bars 2004-2013 and EUR/USD hourly bars, with each real move scaled to the size of a crypto minute. That's real trends, quiet spells, shocks and volume, though not real crypto. Each change had to win on **both** halves of the data: tuned on the first half and checked on the second, which it had never seen.

| 10 runs of $500 per half | First half | Second half |
| --- | --- | --- |
| As it was | +$22.75 (8 of 10 runs up) | +$16.90 (6 of 10 up) |
| Accumulating off | +$36.62 (10 of 10) | +$28.92 (8 of 10) |
| **Accumulating off + no quick-flip slice** | **+$39.90 (10 of 10)** | **+$34.80 (8 of 10)** |

With $2,000 the same change went from +$171 to +$246 on the first half and from +$70 to +$84 on the second, with 7 of 10 runs up instead of 5. At $100 nothing changed: those slices are below Kraken's minimum order anyway. Both features sell part of a winner early. Real prices trend, so a coin that keeps rising doesn't dip back to the buy-back price, and the slice is simply gone, plus two extra fees. A higher conviction bar, waiting longer before trading a coin again, stricter or looser patient exits, a 4x-fees minimum target and a 3x-fees minimum for strategy exits were also tried. None helped on both halves, so they stay as they are.

Both switched back on by request earlier; they're off again because the evidence says they cost money. `TZ_ACCUMULATE=true` and `TZ_FLIP_BANK_FRACTION=0.25` bring them back. These are still simulations: no past data guarantees future profit.

### 🧠 All trading strategies

The Quant has **ten strategies**. Switch them on or off in **Settings → Trading strategies** (or `TZ_STRATEGIES`); your choice is remembered and applies to every linked account.

| Family (one vote each) | Strategies |
| --- | --- |
| Trend-following | **Momentum** (fast vs slow average), **MACD** (histogram), **On-balance volume** (is volume flowing in?), VWAP (above or below the volume-weighted price), 50-bar trend, Rate of change (10 bars) |
| Buying dips | **Buying dips** (RSI), Bollinger Bands (outside the bands), Stochastic (%K under 20 / over 80) |
| Breakouts | **Breakouts** (through the 20-bar high or low) |

The strategies in **bold** are on. The other five are available but off. Each family's vote is the average of its strategies that have an opinion right now (one sitting inside its bands abstains), so ten strategies don't give ten vetoes. The Coach keeps every strategy's own record and trusts the winners more, and the card shows that record. Alongside these, the meme breakout snipes, volume snipes, Pullback Hunter, Flipper and Stacker keep running.

Real price history (20 runs of $500 per half):

| | First half | Second half |
| --- | --- | --- |
| The original three | +$350 | +$287 |
| **+ MACD and on-balance volume (on)** | **+$398** | **+$295** |
| All ten (first 10 runs) | +$141 | +$116 (vs +$162 / +$150 for the three) |

Adding all ten made less: the extra trend models crowded the score and dulled the clear signals. MACD and on-balance volume looked even better on the first 10 runs than on the next 10, so the gain is real but modest.

### ⏩ Trading faster: tested again, still less profit

Faster settings, each tested against today's team on real price history (20 runs of $500 per half; today's team: +$398 / +$295):

| | First half | Second half |
| --- | --- | --- |
| Faster moving averages (8/21 instead of 12/26) | +$363 | +$253 |
| Fastest averages (6/13) | +$388 | +$239 |
| Re-enter a trend after 3 bars instead of 5 | +$378 | +$281 |
| Lower conviction bar (0.18 instead of 0.22) | +$428 | +$246 |
| Free a stuck slot after 2 h instead of 3 | +$398 | +$295 |
| Slower averages (16/34) | +$346 | +$214 |
| Higher bar (0.26) | +$395 | +$230 |
| Re-enter after 8 bars | +$399 | +$302 (on 20 fresh runs: +$371 / +$221 vs +$376 / +$227 - luck) |

Reacting sooner means reacting to more noise and paying more fees. Nothing beat the settings the bots already use. What does make more money is more risk per trade (**Settings → Real-money risk → Aggressive** about doubled the second-half result, with deeper worst runs) and lower fees (Kraken's rate drops as your monthly volume grows, and the bots plan with the fee really paid). The averages' lengths are now settings (`TZ_EMA_FAST_PERIOD`, `TZ_EMA_SLOW_PERIOD`; 12 / 26).

### 🤝 The best combination: what's on works best together

Every strategy and trade finder was tested both ways against the whole team, on real price history (20 runs of $500 per half). Today's team made **+$398 / +$295**.

| Switching **off** one that's on | First half | Second half |
| --- | --- | --- |
| Pullback Hunter | +$350 | +$115 |
| Flipper (volume flips, best hours) | +$390 | +$260 |
| MACD (OBV only) | +$375 | +$270 |
| On-balance volume (MACD only) | +$350 | +$211 |
| Meme breakout snipes | +$393 | +$292 |
| Volume snipes | +$397 | +$290 |

| Switching **on** one that's off | First half | Second half |
| --- | --- | --- |
| VWAP | +$367 | +$289 |
| 50-bar trend | +$396 | +$223 |
| Rate of change | +$348 | +$224 |
| Bollinger Bands | +$313 | +$243 |
| Stochastic | +$303 | +$251 |
| Skims | +$351 | +$237 |
| Quick-flip slices | +$294 | +$167 |
| Accumulating | +$300 | +$274 |
| Profit sales at the ask (maker exits) | +$427 | +$263 |
| Fee rule on locked stops | +$308 | +$242 |

Everything that's on helps: switching any of it off made less on both halves. Everything that's off hurts: switching any of it on made less on the half the settings were never tuned on. So the defaults are the best combination found. The trade finders (snipes, volume snipes, Pullback Hunter, Flipper) can be switched in **Settings → Trading strategies** too (`TZ_SNIPE`, `TZ_SURGE_SNIPE`, `TZ_PULLBACK`, `TZ_FLIPPER`). These are tests on past prices, not a promise about future ones.

### 🎯 Volume snipes: every crypto and meme coin

Any crypto or meme coin whose 1-minute bar trades **4 times its usual volume** (`TZ_SURGE_VOLUME_RATIO`) while the price jumps **+0.3% or more** (`TZ_SURGE_MIN_MOVE`) is bought on that bar. Conditions:
- the price must be above its fast average;
- not already wildly overbought (RSI 80, `TZ_SURGE_MAX_RSI`);
- not a bounce inside a falling trend;
- one snipe per coin per surge (`TZ_SURGE_COOLDOWN_BARS`).

The idea then goes through the same checks as any trade: Risk and the advisors (falling Bitcoin, flash crashes, thin markets), the fee rules, the position limits (5 meme coins, 100 trades), and its own stop. Stocks aren't sniped. The meme-coin breakout snipes (`TZ_SNIPE`) still run alongside. `TZ_SURGE_SNIPE=false` turns it off.

On real price history (ten $500 runs per half) such surges were rare, about one snipe per run. At 4× they made no difference on unseen data (+$162 / +$150 vs +$160 / +$150). At 3× or 2.5× they did worse there (+$137). That history is stocks and currencies, not meme coins, whose surges are bigger and more frequent. Whether sniping them pays will show in real trading.

### 💰 Every profit sale keeps more than its fees

A trade that's sold to take profit must keep **more profit, after both fees, than the fees it paid** (`TZ_PROFIT_OVER_FEES=1`; 2 would mean twice the fees). Example: a $500 trade on Kraken pays about $2 to buy and $2 to sell, so it's only sold for profit once it would keep more than $4 after those fees (about +1.6% on the price). That covers every sale made for profit:
- take-profits, profit slices, the runner's first sale;
- freeing a slot from a trade that's gone nowhere;
- the Skimmer's banking and its day-trade time limit;
- changes of mind on a winner (these need twice the fees);
- **a losing trade that came back** (`TZ_FEE_RULE_RESCUE`): it's closed once it beats its fees, not at +0.3%.

The Risk Manager checks this rule again on every profit sale and turns down any that don't pass. The trade keeps running with its stop.

**Stop-losses are never held back.** Stops, early cuts, Close all trades and Emergency stop always sell. A stop that has locked in profit sits at break-even plus both fees, so it never turns a winner into a loss after fees. Raising those stops to "the fees again" as well was tested (`TZ_FEE_RULE_STOPS`, off): the tighter stops were hit more often.

Real price history (ten $500 runs per half):

| | First half | Second half | Both |
| --- | --- | --- | --- |
| Before (a profit sale only had to beat $0) | +$170 | +$120 | +$290 |
| The rule on profit sales + comebacks (on) | +$160 | +$150 | **+$310** |
| ...and on locked stops too | +$166 | +$84 | +$250 |

A price that jumps straight through a stop, or a fill worse than expected, can still close a trade for less, so this is a rule for every planned sale, not a guarantee on every fill.

### ⚡ Faster skimming and flipping: tested, and why it's not on

Replayed on real price history (ten $500 runs per half, real-money Kraken fees), trading faster made **less** money every time:

| | First half | Second half |
| --- | --- | --- |
| **The team as it is** | **+$206** | **+$85** |
| + skims (4 at once) | +$160 | +$76 |
| + skims + quick-flip slices | +$123 | +$65 |
| Turbo: all of that, banking at +0.6%, re-entering after 2 bars, 1-hour holds | +$112 | +$53 |

Each flip pays about 0.4% to buy and 0.4% to sell on Kraken's starting tier, so a quick +1% flip keeps only about 0.2%, and the losing flips still cost their full loss plus fees. At Kraken's lower high-volume rate (0.24% a side) the team as it is made +$359 / +$227, and turbo still made less (+$215 / +$133). **Fees, not speed, are the biggest lever.** The fee really paid is measured on every fill and plans adjust (Fee Auditor), so as your monthly volume lowers the rate, every trade benefits.

**Profit-taking sells at the ask (maker fee)** were tried as well (`TZ_MAKER_EXITS`, off). The test exchange fills a resting order only when the price trades through it: some sales filled at the lower fee, others missed and went at market after the wait. The result was better on one half (+$198 vs +$141) and worse on the other (+$112 vs +$132), so it stays off. Stops, cuts, Close all trades and Emergency stop always go to market. They now also take any resting order for that coin off the book first. Before, a flatten could wait behind a resting buy, and that buy could still complete afterwards.

No setting turns $500 into 1,000 times its value. Compounding works on whatever the bots really make after fees: about +3% to +17% per $500 run in these tests (and some runs lost). Bigger risk levels raise both the gains and the losses (see *More risk, more reward*).

### Up to 100 trades at a time

The bots can hold up to **100 trades at once** (`TZ_MAX_OPEN_POSITIONS`), on every risk level and at every balance (it was 20, and before that 6 (8 on Aggressive / Bold); small balances were held to 4-6, and at most 4 crypto trades could point the same way). **The real limit is the coins watched:** one trade per coin, so it's about 12 coins plus whatever the Trend Scout finds and qualifies, up to 100 more (`TZ_MAX_DISCOVERED`); fewer on Uphold or Alpaca. Checked with 120 coins watched: about 26 ms of work per one-second cycle, and the screen's live update trimmed to what it shows (106 KB a second instead of 147). Not tested on price history as a change. Still in place:
- at most 5 meme coins at once (`TZ_MAX_MEME_POSITIONS`; 6 on Aggressive, 7 on Bold);
- the Correlation Officer still blocks a third trade that moves exactly like two already held;
- the total-risk cap and the cash on hand still decide how big each trade can be, so more trades means more, smaller trades, never borrowed money.

In a practice run with 24 coins to choose from, the bots held up to 18 trades at once (they open one only when a coin sets up, so 20 isn't always reached). The number of coins on the watchlist is also a limit: Kraken lists plenty, but Uphold and Alpaca give the bots only about 9-12 coins.

### 🔥 More risk, more reward: the Bold level

There are three real-money risk levels (**Settings → Your goal → Real-money risk**; a level above Normal needs **RISK** typed; `TZ_RISK_PROFILE`):

| | Normal | Aggressive | Bold |
| --- | --- | --- | --- |
| Risked per trade (to the early cut) | 2.5% | 4% | 6% |
| Biggest single position | 35% | 50% | 70% |
| All open trades' risk together | 10% | 20% | 30% |
| Stop for the day after a loss of | 5% | 10% | 15% |
| Stop everything after a drop of | 15% | 30% | 40% |
| Stop if below what you put in by | 15% | 35% | 50% |
| Real price history, first half (10 × $500) | +$206 | +$196 | +$333 |
| Real price history, second half (never tuned on) | +$85 | +$170 | +$176 |
| Worst single run | -$16 (-3%) | -$30 (-6%) | -$45 (-9%) |

Going from Normal to Aggressive about doubled the result on the unseen half. Going from Aggressive to Bold added only $6 there, but made the worst run 50% deeper. A still bigger level (8% a trade, a single trade up to the whole balance) was also tested: +$182 on the unseen half, with a worst run of -$54 (-11%). It isn't offered, because the extra risk bought almost nothing. Every level keeps a stop and the early cut on every trade, never borrows, and never touches the hold stack. Practice money always uses Normal.

### 💵 The whole cash balance trades (your hold stack excepted)

With the -33% stop, trades used to be sized as if every one would fall all the way to -33%. That made each trade about 7% of the balance, and the 10% cap on money at risk kept about two thirds of the cash idle. Now (`TZ_FULL_CASH`, on) a trade is sized for the **patient cut**: -5% (meme coins -12%), where a falling trade is closed early when momentum and volume say it isn't coming back. The risk cap is counted the same way. So the whole cash balance can be at work: a Normal real-money trade is up to 35% of the balance (Aggressive 50%), and a few trades together can use all of it.

What stays the same:
- the -33% stop on every trade, as the last-resort backstop;
- never more than the cash on hand, and never borrowing;
- the coins in your hold stack (and coins you held before) are never traded;
- the daily-loss, drawdown and capital-floor stops.

The Stacker only spends cash that trading isn't using, so trading comes first.

Replayed on real price history (same test as above, ten $500 runs per half, stacking off so only sizing differs):

| | First half | Second half |
| --- | --- | --- |
| Sized by the -33% stop (as it was) | +$39.90 (10 of 10 up) | +$34.80 (8 of 10 up) |
| **Sized by the patient cut** | **+$206.16 (10 of 10 up)** | **+$85.48 (7 of 10 up)** |

About five times as much money is working, so the swings are bigger too: the worst run went from -$2.20 to -$16.40 (-3.3%). With most of the cash in coins, a sharp market-wide drop costs more before the daily-loss stop (-5% Normal) closes everything. `TZ_FULL_CASH=false` goes back to sizing by the -33% stop.

### 🪙 The Stacker: a hold stack on the side (never sold, no stop-loss)

The Stacker slowly builds a long-term hold of **SOL, XRP, DOGE, ETH and Onyxcoin (XCN)** (`TZ_STACK_COINS`), separate from the trading:

- **XCN is stack-only** (`TZ_STACK_ONLY_COINS`): its price is watched so the Stacker can buy it, but the trading bots never open a trade in it. It's a small coin that swings hard, so it's held, not flipped with the trading balance. It's listed on Coinbase and Kraken; on a venue that doesn't list it (Alpaca), it's simply skipped. Moved out of the stack by hand ("Trade it"), the bots manage that position like any other.

- **Funding:** a small budget builds up over time: **1% of the trading balance a day** (`TZ_STACK_DAILY_PCT`, or a fixed `TZ_STACK_DAILY_USD`) plus **a quarter of every winning trade's profit** (`TZ_STACK_PROFIT_SHARE`).
- **Buying:** once the budget covers the exchange's smallest order (at least $5, `TZ_STACK_MIN_BUY_USD`; up to $25 a buy, `TZ_STACK_MAX_BUY_USD`), it buys one coin: whichever is furthest below its share of the stack. It prefers a coin that isn't running hot (RSI under 55); if every coin has been hot for 6 hours, it buys anyway, because the point is buying slowly, not timing.
- **Never sold, no stop-loss.** Held coins are not part of the trading book: no bot ever sells them, and Close all trades and Emergency stop leave them alone. On a real-money account they sit in your exchange account like coins you bought yourself, and the records' check leaves them alone. They're yours to keep, or to sell yourself.
- **Not at the trading team's expense.** It only spends free cash - all of it if need be (`TZ_STACK_KEEP_CASH`, 0; it used to leave a quarter free) - stops growing past half of everything (`TZ_STACK_MAX_SHARE`), and waits while trading is paused or stopped. Money moved into the stack is shown as moved, not as a trading loss, so the trading balance, its profit and its loss limits stay honest.
- **Seeing it:** the **Hold stack** card shows each coin's amount, what it cost and what it's worth now; the home screen adds "plus $X in your hold stack" under the balance. **Pause stacking** stops new purchases (what you hold stays held), and stays paused after a restart. Practice money and real money keep separate stacks.

Coins held without a stop can fall a long way. Buying slowly over time spreads that risk out but doesn't remove it.

**Moving coins by hand.** On the Hold stack card:
- **Trade it / Trade half** moves stacked coins to the bots. They become a normal trade from today's price, with a stop, a target and every usual rule, and the bots trade that coin again.
- **Hold it / Hold half** (under "Move coins from the bots' trades") moves coins from an open trade into the stack. They're booked at today's price, so that part of the trade's gain or loss is settled; you choose this, so it's allowed even under never-sell. From then on the bots never sell those coins.

The coins never leave your exchange account and nothing is bought or sold there. Your cash doesn't change, a move isn't counted as profit or loss, and on a real-money account the records and the cash check stay matched to the exchange. A move waits while an order for that coin is still working, and coins can't be moved out while the bots already have a trade open in that coin. Every move is kept in the records forever. The learning bots ignore moves, since they aren't trades.

### The real-money balance follows your account

Every half minute the bots compare their cash with what the exchange reports. Money you **add** or **take out** at the exchange is picked up as a deposit or withdrawal, so it's never counted as profit or loss, and today's loss limits move with it. The difference must show up on two checks in a row with no order in flight, and differences under $1 (or 0.5% of the balance) are treated as rounding. With a capital cap (`TZ_LIVE_MAX_CAPITAL`) only money taken out is followed, so the bots never count on cash that's gone. On Kraken, the account total includes coins you hold yourself, so it is no longer compared with the bots' balance (that comparison used to raise a warning every half minute).

The dashboard also reconnects by itself when a phone wakes up, changes network or goes offline for a while: if no update arrives for 20 seconds it opens a fresh connection, so the balance never sits frozen.

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

Every trade's stop-loss sits **20% below the buy price** (`TZ_WIDE_STOP_PCT`, was 33%; 20% above for a short), so ordinary dips never stop a trade out. With never-sell on, the **loss floor** (`TZ_MAX_LOSS_PCT`, 20%) is what lets that stop sell a loser: tested on real price history over 3.5-day windows (16 per market, tuning / unseen half), it changed nothing when prices rose or went sideways - nothing fell that far - and lost about a fifth less when they fell (−$598 vs −$755 / −$417 vs −$532). A 20% stop *without* the floor changed nothing at all (never-sell held the trade anyway). With the floor in place, a looser underwater cap (25%) earned more when prices rose but did worse on the unseen data, so it stays at 15%. Three things keep this from risking more money:

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

**SOL and XRP at any balance** (`TZ_ALWAYS_TRADE`, default `SOL,XRP`): these two skip the ladder - the bots may trade them from the first dollar, and they're always watched (added to the coins list if missing). They're still **stacked** too: the hold stack keeps buying them slowly on the side, and the hold stack's coins and the trading's coins are kept apart (the bots never sell the stack's). Not tested on price history as a change: every SOL or XRP trade still goes through all the usual checks, stops and limits.

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
