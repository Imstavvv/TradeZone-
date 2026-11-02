# TradeZone

A multi-bot trading platform. Eight specialised bots, including an AI analyst powered by xAI's Grok,, each with its own role, work as a desk. They talk over an async event bus, and you supervise them from a real-time web dashboard with pause, flatten and kill-switch controls.

It runs in **paper mode by default**, trading **live market prices** with simulated fills and no money at risk. The bots learn on the same data they'll later trade live. Live trading through Alpaca is supported, but you have to opt in explicitly (see [Live trading](#live-trading)).

## The bot team

| Bot | Role | What it does |
| --- | --- | --- |
| **Scout** | Market data and signal prep | Ingests every tick, rejects bad data, detects shock moves and stale feeds, and computes EMA, RSI, volatility, ATR and Donchian channels |
| **Strategist** | Market regime analysis | Classifies each symbol as *trending*, *ranging* or *volatile* (Kaufman efficiency ratio and volatility ratio, with hysteresis) |
| **Quant** | Strategy and signal generation | Runs momentum, mean-reversion and breakout models, weights them by the Strategist's regime call, and only proposes a buy when the team reaches consensus (see below) |
| **Grok** | AI analyst (xAI Grok) | Sends every proposed new position, with indicators, regime, recent prices and portfolio context, to Grok for an independent second opinion. In `veto` mode, trades Grok doesn't approve are never placed |
| **Risk Manager** | Risk control and position sizing | Gates every signal: kill switch, pause, exposure, concentration, max positions, cooldowns, order-rate limit. Sizes positions by volatility (risk to stop) and halves size in volatile regimes. Trips the kill switch on daily-loss or drawdown limits |
| **Executor** | Order execution and routing | Runs pre-trade checks, sends orders to the broker, blocks duplicates, enforces reduce-only exits, verifies every fill, and cancels stale orders |
| **Portfolio Manager** | Positions, P&L and exits | Books fills (average-cost accounting), marks positions to market, manages stop-loss, take-profit, trailing stops and profit protection, and reconciles against the broker |
| **Sentinel** | Audit, compliance and performance | Journals every decision to SQLite, keeps an independent audit ledger that is checked against the books every cycle, computes Sharpe, drawdown, win rate and profit factor, and raises alerts when any bot errors |

### How a trade flows

```
market.tick ─► Scout ─► market.snapshot ─► Strategist ─► (regime)
                                     └──► Quant ─► signal.proposed ─► Grok (veto) ─► Risk Manager
                                                                         │ approve / reject
                                                     order.approved ◄────┘
                                                        │
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

With realistic fees, the bundled strategies came out roughly break-even in offline simulation (−1.1% to +0.6% across 4 seeds, max drawdown ≤1.4%, no winner closing as a loss). Synthetic data says little about real markets. Paper-trade on live prices for a good while before going live.

## Market data: paper trades live prices

By default (`TZ_FEED=auto`), paper trading uses **real market data**. If Alpaca keys are set it uses Alpaca (stocks and crypto, polled). Otherwise it uses Coinbase's public real-time stream (crypto, no account needed).

Indicators and signals run on **1-minute bars** (`TZ_BAR_SECONDS`), pre-loaded from history on Coinbase. Stops, targets and profit locks react to every trade. On Alpaca there's no history pre-load yet, so expect a 30-bar warm-up (shown in a banner).

`TZ_FEED=sim` (synthetic prices) remains available for offline demos, the tests and `python -m tradezone sim`.

## Buys need team consensus

A buy only happens when the team agrees. Each voter says yes, no or abstain:

| Voter | Votes yes when… | Votes no when… |
| --- | --- | --- |
| Momentum model | fast trend is up | fast trend is down |
| Mean-reversion model | price is oversold | price is overbought |
| Breakout model | price breaks the 20-bar high | price breaks the 20-bar low |
| Strategist | trending up, or ranging and oversold | volatility storm, or trending down |

**Rule:** no voter may say no, and at least `TZ_CONSENSUS_MIN_YES` (default 2) must say yes. After that, **Grok** must approve (if connected) and the **Risk Manager** must approve. The Quant re-checks on every bar while its view holds, so a buy goes ahead as soon as the last objection clears. Each signal's votes appear in Team activity, for example `[momentum:✓ mean-rev:– breakout:✓ strategist:✓]`. Selling to close a position needs no vote.

## Stop-loss placement and risk management

Stops go where most traders put them, **just beyond the most recent swing point**:

* **Long:** below the lowest low of the last 10 bars (the last dip buyers defended), minus a 1×ATR buffer so ordinary noise doesn't tag it. Shorts mirror this above the swing high.
* **Off round numbers:** if the stop lands on or near a round number (100, 105, 65,000…), where stops cluster and get hunted, it moves just beyond it.
* **Never too tight:** at least 1.5×ATR from entry.
* **Never too wide:** if the swing stop is more than 4×ATR away, the trade is **skipped** rather than taken with oversized risk.
* **Target:** 2× the stop distance (2:1 reward-to-risk). The trade is skipped if resistance is less than 1R away, because it would have no room to work.
* **Fallback:** if there's no swing level yet, the stop is 2×ATR. `TZ_STOP_METHOD=atr` switches everything to plain ATR stops.

Risk management layered on top:

| Rule | Default |
| --- | --- |
| Risk per trade: position sized so hitting the stop loses at most this | 1% of equity (half that in volatile regimes) |
| Portfolio heat: total loss if *every* open stop is hit | 5% of equity |
| Max position size / total exposure / open positions | 20% / 100% / 5 |
| Daily loss / drawdown kill switch (flattens everything) | −5% / −15% |
| Profit protection after +0.5R | stop to break-even + costs, then locks 50% of peak profit |

Every approved trade's reason in Team activity and Orders shows its stop, how it was placed, the target and the money at risk. For example: `stop 98.8 (below swing low 99.2 - 1 ATR buffer), target 102.4, risk 412.50`.

## Profit protection

**No system can guarantee every trade ends in profit.** A price can fall right after a buy, before the trade is ever green. Removing stop-losses wouldn't change that; it would only turn small losses into large ones. What TradeZone enforces is the next best thing: **once a trade has been in profit, it's not allowed to turn into a loss.**

1. **Break-even lock:** when open profit reaches 0.5R (half the distance from entry to the initial stop), the stop moves above break-even plus round-trip fees, slippage and a cushion sized to the symbol's typical tick move. From then on it locks in 50% of the best open profit seen. These positions show a 🔒 in the dashboard.
2. **Partial take-profit:** at +1R, half the position is sold to bank the gain. The rest rides with the locked stop toward the target.
3. **Strategy exits only in profit:** a bearish model signal closes a position only if that nets a profit. Losing positions are left to the stop-loss, which still cuts them. Your Flatten button and the kill switch always work regardless.

The **Profit protection** tile counts any winner that still closed at a loss. That can only happen if the price jumps straight through the stop between two ticks, and each case raises an alert. In offline simulation runs (4 seeds × 3,000 steps), the count was 0 and the win rate went from about 40% to about 70%. That's synthetic data, not a promise about real markets.

## Grok (xAI) connection

Set an xAI API key to connect the Grok bot:

```bash
TZ_GROK_API_KEY=xai-...        # XAI_API_KEY also works
TZ_GROK_MODEL=grok-4           # any chat model your xAI account can use
TZ_GROK_MODE=veto              # veto (default when a key is set) | advisory | off
TZ_GROK_FAIL_OPEN=false        # if Grok is unreachable, reject new entries (the safe default)
```

* **veto:** new positions need Grok's approval. Its confidence is blended into the position size. Signals that go stale while waiting for Grok (`TZ_GROK_MAX_SIGNAL_AGE_S`) are dropped.
* **advisory:** Grok's opinion is logged and shown, but it doesn't block trades.
* **Exits never wait for Grok.** Stops, targets, flattening and the kill switch always go straight through.

Use **Test Grok** in the dashboard header (or `POST /api/grok/ping`) to check the connection.

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

## Getting a web address for the dashboard

The dashboard is served by TradeZone itself. **It's ready the moment the server starts.** The header shows `● live` once your browser connects, and the Scout card counts down the warm-up (30 bars, about 7–8 minutes on live data) before the bots start trading.

**On your computer:** run `python -m tradezone serve` and open http://127.0.0.1:8000.

**On the internet (Render, about 5 minutes):**

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, then pick the repo. `render.yaml` sets up an always-on service with a disk for the trade journal, and generates a random `TZ_API_TOKEN`.
3. When the deploy turns **Live**, open the `https://tradezone-….onrender.com` URL Render shows. Paste the API token when the page asks for it (it's under Environment in Render).
4. Optionally add your Alpaca / Grok keys under Environment. Never commit keys.

Any Docker host works the same way (`docker run -p 8000:8000 --env-file .env -v tz-data:/data tradezone`). Always set `TZ_API_TOKEN` when the dashboard is reachable from the internet, because it controls real-money trading.

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

```bash
TZ_FEED=alpaca                # or coinbase (crypto). Live can't trade on simulated prices.
TZ_ALPACA_LIVE_KEY=...        # your Alpaca *live* account keys
TZ_ALPACA_LIVE_SECRET=...
TZ_LIVE_MAX_CAPITAL=500       # optional: bots trade at most $500 of the account
TZ_API_TOKEN=<long random>    # required if the dashboard is reachable beyond localhost
```

With live keys set, the **Go LIVE** button in the dashboard header becomes active. Without them, it's greyed out, and hovering over it says what's missing.

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
| API token set when exposed beyond localhost | Nobody else on the network can flip you to live |

Paper and live keep **separate books**, so P&L, trades and the audit ledger never mix, and paper resumes where it left off. In live mode, Portfolio Manager checks positions against the broker every 30 cycles, and any mismatch pauses trading. Wallet deposits are recorded but not added to trading capital (your broker balance is the source of truth), and withdrawals are `manual_required`.

To start directly in live mode (for example, on a server after a restart), set `TZ_MODE=live` and `TZ_LIVE_CONFIRM="I UNDERSTAND THE RISKS"`. If any check fails at startup, the desk stays in paper and raises a critical alert.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Liveness (no auth) |
| GET | `/api/state` | Full desk state: portfolio, bots, orders, trades, events |
| GET | `/api/bots` | Bot team status |
| POST | `/api/bots/{name}` | `{"enabled": false}` disables a bot |
| POST | `/api/control/{action}` | `pause`, `resume`, `flatten`, `kill`, `reset` |
| GET | `/api/journal?topic=order.filled` | Audit trail from SQLite |
| GET | `/api/mode` | Preview a paper/live switch: blockers, live account, trading capital |
| POST | `/api/mode` | `{"mode": "live", "confirm": "LIVE"}` or `{"mode": "paper", "flatten": true}` |
| POST | `/api/grok/ping` | Test the Grok connection |
| GET / POST | `/api/wallets` | List wallets and transfers / link a wallet `{chain, address, tag?, label?, no_tag_confirmed?}` |
| DELETE | `/api/wallets/{id}` | Unlink a wallet |
| GET | `/api/deposits/info` | Deposit address and deposit tag for each wallet |
| POST | `/api/deposits` | Record an incoming deposit `{chain, asset, amount, tx_hash, tag?, from_address?}` |
| POST | `/api/deposits/{id}/credit` | Attribute a held deposit `{wallet_id}` |
| POST | `/api/withdrawals` | Withdraw to a linked wallet `{wallet_id, asset, amount}` |
| WS | `/ws` | Live state stream (`?token=` when an API token is set) |

When `TZ_API_TOKEN` is set, every endpoint except health requires `Authorization: Bearer <token>`.

## Project layout

```
tradezone/
  core/        event bus, models, indicators, portfolio ledger
  bots/        the 8-bot team (incl. Grok AI analyst)
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
