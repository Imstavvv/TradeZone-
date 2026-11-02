# TradeZone

A multi-bot trading platform. Eight specialised bots, including an AI analyst powered by xAI's Grok,, each with its own role, work as a desk. They talk over an async event bus, and you supervise them from a real-time web dashboard with pause, flatten and kill-switch controls.

It runs in **paper mode by default**: simulated or real market prices, simulated fills, no money at risk. Live trading through Alpaca is supported, but you have to opt in explicitly (see [Live trading](#live-trading)).

## The bot team

| Bot | Role | What it does |
| --- | --- | --- |
| **Scout** | Market data and signal prep | Ingests every tick, rejects bad data, detects shock moves and stale feeds, and computes EMA, RSI, volatility, ATR and Donchian channels |
| **Strategist** | Market regime analysis | Classifies each symbol as *trending*, *ranging* or *volatile* (Kaufman efficiency ratio and volatility ratio, with hysteresis) |
| **Quant** | Strategy and signal generation | Runs momentum, mean-reversion and breakout models, weights them by the Strategist's regime call, and proposes signals with a conviction score |
| **Grok** | AI analyst (xAI Grok) | Sends every proposed new position, with indicators, regime, recent prices and portfolio context, to Grok for an independent second opinion. In `veto` mode, trades Grok doesn't approve are never placed |
| **Risk Manager** | Risk control and position sizing | Gates every signal: kill switch, pause, exposure, concentration, max positions, cooldowns, order-rate limit. Sizes positions by volatility (risk to stop) and halves size in volatile regimes. Trips the kill switch on daily-loss or drawdown limits |
| **Executor** | Order execution and routing | Runs pre-trade checks, sends orders to the broker, blocks duplicates, enforces reduce-only exits, verifies every fill, and cancels stale orders |
| **Portfolio Manager** | Positions, P&L and exits | Books fills (average-cost accounting), marks positions to market, manages stop-loss, take-profit and trailing stops, and reconciles against the broker |
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

## Quick start

```bash
pip install -r requirements.txt
python -m tradezone serve            # dashboard at http://127.0.0.1:8000
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
| Demo (default): synthetic market, paper fills | nothing |
| Real crypto prices, paper fills (no account needed) | `TZ_FEED=coinbase TZ_SYMBOLS=BTC-USD,ETH-USD,SOL-USD` |
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
