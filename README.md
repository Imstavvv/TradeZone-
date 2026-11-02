# TradeZone

A multi-bot trading platform. Seven specialised bots, each with its own role, work as a desk. They talk over an async event bus, and you supervise them from a real-time web dashboard with pause, flatten and kill-switch controls.

It runs in **paper mode by default**: simulated or real market prices, simulated fills, no money at risk. Live trading through Alpaca is supported, but you have to opt in explicitly (see [Live trading](#live-trading)).

## The bot team

| Bot | Role | What it does |
| --- | --- | --- |
| **Scout** | Market data and signal prep | Ingests every tick, rejects bad data, detects shock moves and stale feeds, and computes EMA, RSI, volatility, ATR and Donchian channels |
| **Strategist** | Market regime analysis | Classifies each symbol as *trending*, *ranging* or *volatile* (Kaufman efficiency ratio and volatility ratio, with hysteresis) |
| **Quant** | Strategy and signal generation | Runs momentum, mean-reversion and breakout models, weights them by the Strategist's regime call, and proposes signals with a conviction score |
| **Risk Manager** | Risk control and position sizing | Gates every signal: kill switch, pause, exposure, concentration, max positions, cooldowns, order-rate limit. Sizes positions by volatility (risk to stop) and halves size in volatile regimes. Trips the kill switch on daily-loss or drawdown limits |
| **Executor** | Order execution and routing | Sends approved orders to the broker, blocks duplicates, enforces reduce-only exits, tracks partial fills and cancels stale orders |
| **Portfolio Manager** | Positions, P&L and exits | Books fills (average-cost accounting), marks positions to market, manages stop-loss, take-profit and trailing stops, and reconciles against the broker |
| **Sentinel** | Audit, compliance and performance | Journals every decision to SQLite, computes Sharpe, drawdown, win rate and profit factor, and raises alerts when any bot errors |

### How a trade flows

```
market.tick ─► Scout ─► market.snapshot ─► Strategist ─► (regime)
                                     └──► Quant ─► signal.proposed ─► Risk Manager
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

## Live trading

> **Warning.** Live mode places real orders with real money. The bundled strategies are examples. They're tested only on synthetic data, which says nothing about real-world profitability. Run on paper for a long time first, start small, and use a dedicated account.

Live mode refuses to start unless **all** of the following are set:

```bash
TZ_MODE=live
TZ_BROKER=alpaca
TZ_FEED=alpaca            # live mode can't trade on simulated prices
TZ_ALPACA_KEY=...
TZ_ALPACA_SECRET=...
TZ_LIVE_CONFIRM="I UNDERSTAND THE RISKS"
TZ_API_TOKEN=<long random string>   # protects the control API and dashboard
```

At startup, the desk syncs cash and equity from the broker account. It does **not** import existing positions, so start from a flat account. The ledger is reconciled against broker equity every 30 cycles, and a drift above 2% raises an alert.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Liveness (no auth) |
| GET | `/api/state` | Full desk state: portfolio, bots, orders, trades, events |
| GET | `/api/bots` | Bot team status |
| POST | `/api/bots/{name}` | `{"enabled": false}` disables a bot |
| POST | `/api/control/{action}` | `pause`, `resume`, `flatten`, `kill`, `reset` |
| GET | `/api/journal?topic=order.filled` | Audit trail from SQLite |
| WS | `/ws` | Live state stream (`?token=` when an API token is set) |

When `TZ_API_TOKEN` is set, every endpoint except health requires `Authorization: Bearer <token>`.

## Project layout

```
tradezone/
  core/        event bus, models, indicators, portfolio ledger
  bots/        the 7-bot team
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
