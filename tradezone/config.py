"""Runtime configuration, loaded from environment variables (prefix ``TZ_``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace

LIVE_CONFIRM_PHRASE = "I UNDERSTAND THE RISKS"
#: what the operator types in the dashboard to switch to live trading
LIVE_SWITCH_WORD = "LIVE"

#: "trusted" meme coins: the large, long-listed ones on major US exchanges (quick flips happen here)
MEME_COINS = ["DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI"]
#: other well-known meme coins: added automatically when Kraken lists them against USD AND they trade
#: enough every day that a position can be sold again (see TZ_MIN_MEME_VOLUME_USD)
MEME_CANDIDATES = ["TRUMP", "PENGU", "FARTCOIN", "POPCAT", "MEW", "BOME", "TURBO", "PNUT", "MOG", "BRETT", "GIGA",
                   "SPX", "MOODENG", "NEIRO", "GOAT", "PONKE", "MYRO", "WEN", "BABYDOGE", "DOG", "TOSHI", "MEME",
                   "CHILLGUY", "AI16Z", "MELANIA", "SUNDOG", "HIPPO", "PUPS", "BAN", "SLERF"]
#: coin sizes for the growth ladder (by market size; everything not listed is a "smaller coin")
LARGE_COINS = {"BTC", "ETH"}
MID_COINS = {"SOL", "XRP", "BNB", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "TRX", "TON", "XLM", "ATOM", "NEAR",
             "UNI", "ETC", "XMR", "HBAR", "APT", "ICP", "FIL", "AAVE"}
#: smaller altcoins added (with Kraken prices) when Kraken lists them and they trade enough
SMALL_CANDIDATES = ["ALGO", "ARB", "OP", "SUI", "SEI", "INJ", "TIA", "RENDER", "FET", "JUP", "PYTH", "ONDO", "STX",
                    "IMX", "GRT", "MANA", "SAND", "CRV", "LDO", "KAS", "JASMY", "CHZ", "GALA", "ENA", "WLD"]
#: default list on the free Coinbase feed: majors plus the trusted meme coins
CRYPTO_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "AVAX-USD", "LINK-USD",
                  "DOGE-USD", "SHIB-USD", "PEPE-USD", "BONK-USD", "WIF-USD", "FLOKI-USD"]
#: coins Alpaca (the real-money broker) can trade; others are practice-only
ALPACA_CRYPTO = {"AAVE", "AVAX", "BAT", "BCH", "BTC", "CRV", "DOGE", "DOT", "ETH", "GRT", "LINK", "LTC",
                 "MKR", "PEPE", "SHIB", "SOL", "SUSHI", "UNI", "XRP", "XTZ", "YFI"}
#: currencies Kraken trades against the US dollar as cash pairs (EUR/USD, GBP/USD, AUD/USD). Pairs with the dollar
#: first (USD/JPY, USD/CAD, USD/CHF) are the other way round and aren't supported: the bots buy things priced in USD.
FOREX = {"EUR", "GBP", "AUD"}
#: default list on the Alpaca feed: crypto only (stocks can still be added with TZ_SYMBOLS)
ALPACA_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "AVAX-USD", "LINK-USD",
                  "DOGE-USD", "SHIB-USD", "PEPE-USD"]
#: US stocks / ETFs the Alpaca team trades next to its crypto (big, liquid, fractional): tested on real price
#: history, long only, they added a little profit over crypto alone (more when prices rise or go sideways)
ALPACA_STOCKS = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT"]
#: synthetic demo feed (offline demos and tests)
MIXED_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "AAPL", "NVDA", "TSLA"]


def _env(name: str, default):
    raw = os.environ.get(f"TZ_{name.upper()}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


#: the Aggressive risk level (real money only). Every trade still has a stop; nothing is borrowed.
AGGRESSIVE = {"risk_per_trade": 0.04, "max_position_pct": 0.50, "max_portfolio_heat": 0.20, "meme_risk_mult": 0.75,
              "daily_loss": 0.10, "drawdown": 0.30, "floor": 0.35, "max_open_positions": 100, "extra_memes": 1,
              "bar_cut": 0.03, "day_lock_trigger": 0.04, "day_giveback": 0.6}
#: the Bold risk level (real money only): bigger again. Tested on real price history it made a little more than
#: Aggressive on unseen data, with clearly bigger worst losses. Every trade still has a stop; nothing is borrowed.
BOLD = {"risk_per_trade": 0.06, "max_position_pct": 0.70, "max_portfolio_heat": 0.30, "meme_risk_mult": 0.75,
        "daily_loss": 0.15, "drawdown": 0.40, "floor": 0.50, "max_open_positions": 100, "extra_memes": 2,
        "bar_cut": 0.04, "day_lock_trigger": 0.06, "day_giveback": 0.65}
#: real-money risk levels above Normal
PROFILES = {"aggressive": AGGRESSIVE, "bold": BOLD}

# real-money venues -> how the app names them
VENUES = {"alpaca": "Alpaca", "kraken": "Kraken", "uphold": "Uphold"}

@dataclass
class RiskLimits:
    risk_per_trade: float = 0.015  # fraction of equity risked to the stop (1.5%)
    max_position_pct: float = 0.25  # max notional per symbol as fraction of equity
    max_gross_exposure: float = 1.0  # max total notional as multiple of equity
    max_open_positions: int = 100  # trades open at once (one per coin: the coins watched are the real limit)
    max_daily_loss_pct: float = 0.05  # kill switch trips beyond this daily loss
    max_drawdown_pct: float = 0.15  # kill switch trips beyond this drawdown from peak
    max_orders_per_minute: int = 30
    max_portfolio_heat: float = 0.07  # total risk to all stops, as a fraction of equity
    min_signal_strength: float = 0.35
    symbol_cooldown_s: float = 10.0
    # Buys need team consensus: no voter may object and at least this many must actively agree.
    # Voters: momentum, mean-reversion and breakout models plus the Strategist (then Grok, then Risk).
    consensus_min_yes: int = 2
    # the share of the voters (the momentum, dip and breakout strategy families + the Strategist: 4 in all) that must
    # say yes, as 0-1 (0.5 = half of them = 2 of 4, the same as consensus_min_yes 2). 0 = use consensus_min_yes.
    # Any "no" still blocks the trade either way.
    consensus_min_share: float = 0.0
    fractional: bool = True


@dataclass
class Settings:
    # Startup mode. The desk always starts in paper unless this is "live" AND TZ_LIVE_CONFIRM is set.
    # Day to day, switch with the dashboard's Go LIVE / Back to PAPER button instead.
    mode: str = "paper"  # paper | live
    # Price feed. "auto" = real market data: Alpaca if keys are set, else Coinbase (crypto, no key).
    # "sim" (synthetic prices) is only for offline demos and tests.
    feed: str = "auto"  # auto | coinbase | kraken | alpaca | sim (auto + a Kraken account = Kraken's own prices)
    feed_choice: str = ""  # what was asked for before "auto" was resolved (set by validate)
    broker: str = "paper"  # broker used in PAPER mode: paper (internal simulator) | alpaca (Alpaca paper account)
    symbols: list[str] = field(default_factory=list)  # empty = sensible default for the feed
    # Indicators are computed on bars of this many seconds when trading off real data (ticks still
    # drive stops/targets in real time). 1-second ticks are mostly noise.
    bar_seconds: float = 60.0  # 1-minute bars; history is pre-loaded so trading starts immediately
    starting_cash: float = 100_000.0
    cycle_seconds: float = 1.0
    # Realistic trading costs (paper fills use these; profit locks and the fee filter account for them).
    # Defaults match Alpaca's base tier: crypto 0.25% taker, US stocks commission-free.
    crypto_fee_bps: float = 25.0
    stock_fee_bps: float = 0.0
    slippage_bps: float = 3.0
    # Skip trades whose profit target is less than this multiple of the round-trip cost.
    min_edge_cost_multiple: float = 3.0
    seed: int = 7
    journal_path: str = ""  # empty: /data/tradezone.db if a /data disk is mounted (Render), else ./tradezone.db
    host: str = "127.0.0.1"
    port: int = 8000
    api_token: str = ""  # optional master key for scripts/recovery (16+ chars if public); sign-in uses the PIN
    # ---- firewall / abuse protection ----
    allowed_ips: list[str] = field(default_factory=list)  # optional allowlist of IPs/CIDRs, e.g. 203.0.113.7,10.0.0.0/8
    rate_limit_per_min: int = 600  # requests per IP per minute
    login_failures_before_ban: int = 5  # wrong tokens within 15 min before the IP is banned
    ban_minutes: float = 30.0  # first ban; repeat offenders are banned twice as long each time (max 7 days)
    trusted_proxies: str = ""
    # ---- app lock (PIN / Face ID) ----
    live_confirm: str = ""
    alpaca_key: str = ""  # Alpaca PAPER account keys (paper broker / market data)
    alpaca_secret: str = ""
    alpaca_live_key: str = ""  # Alpaca LIVE (real money) account keys - enables the Go LIVE button
    alpaca_live_secret: str = ""
    # Kraken (real money, crypto): a key WITHOUT withdraw permission - TradeZone refuses keys that can withdraw
    kraken_key: str = ""
    kraken_secret: str = ""
    maker_entries: bool = True  # real-money buys wait at the bid as a post-only limit (maker fee) before going market
    maker_wait_s: float = 20.0  # ...this long; what isn't filled by then is bought at market
    # real-money profit-taking sells wait at the ask the same way (stops and cuts never do). Off: on real price history
    # the lower fee was outweighed by sells that missed their price and then went at market lower
    maker_exits: bool = False
    kraken_fee_bps: float = 40.0  # Kraken's taker fee per side at the starting tier (0.40%); fills report the real fee
    kraken_memes: bool = True  # with Kraken prices: add the meme coins Kraken lists (if they trade enough)
    min_meme_volume_usd: float = 1_000_000.0  # a meme coin must trade at least this much a day to be added
    max_meme_coins: int = 12  # watch at most this many meme coins
    # ---- "never buy a coin that can't be sold" (real money; practice too when a real account is linked) ----
    sell_max_spread_pct: float = 0.015  # the bid/ask gap must be under 1.5%...
    sell_min_volume_usd: float = 250_000.0  # ...the coin must trade at least this much a day...
    sell_max_volume_share: float = 0.01  # ...and a position may be at most 1% of a day's trading
    # ---- transfer fees: moving profit out costs a fee, so profit-taking keeps it in mind ----
    cashout_fee_usd: float = 0.0  # what one transfer out costs you (0 = estimate from your linked wallets, else $1)
    transfer_fee_share: float = 0.10  # every profit-taking sale must make at least 10% of one transfer fee
    # ---- Uphold (real money): one bearer token (Personal Access Token or OAuth token) ----
    uphold_token: str = ""
    # Uphold's cost is its spread (built into the rate). Practice trades charge this much per side
    # when Uphold is the real-money venue, so practice results match what real money would do.
    uphold_fee_bps: float = 100.0
    # which venue trades real money: auto | uphold | alpaca | kraken
    # (auto: Uphold if its token is set, else Kraken if its key is set, else Alpaca; linking one in the app picks it)
    live_venue: str = "auto"
    live_max_capital: float = 0.0  # if > 0, live mode trades at most this much USD of the account
    # ---- "don't lose my money": stricter limits whenever real money is trading ----
    live_max_daily_loss_pct: float = 0.05  # real money: stop for the day after -5%
    live_max_drawdown_pct: float = 0.15  # real money: stop after -15% from the peak
    live_capital_floor_pct: float = 0.15  # real money: stop if the account falls 15% below what you put in
    # real money (Normal): more capital per trade than practice
    live_risk_per_trade: float = 0.025  # 2.5% of the balance risked to the (wider, patient) stop
    live_max_position_pct: float = 0.35
    live_max_portfolio_heat: float = 0.10
    # ---- Trend Scout: meme coins over $50k market cap with promising volume, and trending coins ----
    discover_coins: bool = True
    journal_keep_days: float = 2.0  # the Housekeeper clears working notes (trade ideas, turn-downs) older than this
    # coins launched on pump.fun (CoinGecko's pump.fun category), same bar: $50k+ market cap, plenty of volume.
    # They're traded only once the exchange lists them (never through a wallet key: nothing can be withdrawn)
    discover_pump: bool = True
    same_coin_tolerance: float = 0.05  # a discovered coin's price must match the exchange's within 5% (same coin)
    discovery_min_mcap: float = 50_000.0  # meme coins worth at least this much...
    discovery_min_volume: float = 100_000.0  # ...trading at least this much a day...
    discovery_min_turnover: float = 0.05  # ...and at least 5% of the market cap changing hands a day
    max_discovered: int = 100  # coins the Trend Scout may add to the watchlist (each still needs enough volume)
    history_pause_s: float = 1.1  # between price-history requests for newly found coins (Kraken allows ~1 a second)
    # stocks (Alpaca accounts): the day's busiest established stocks from Alpaca's screener
    discover_stocks: bool = False  # all the cash goes to crypto: no stocks found or traded (TZ_DISCOVER_STOCKS=true brings them back)
    max_discovered_stocks: int = 10
    stock_min_price: float = 10.0  # no penny stocks
    stock_min_dollar_volume: float = 50_000_000.0  # at least $50M traded today: easy to buy and sell
    discovery_every_min: float = 30.0
    trending_nudge: float = 0.05  # conviction boost for buying a coin that's trending
    # ---- Flipper: flip trades and the best times to flip ----
    flipper: bool = True
    # ---- Stacker: slowly builds a long-term hold on the side (never sold by the bots, no stop-loss) ----
    stack: bool = True
    # ---- Alpaca: US stocks next to the crypto, and the day-trade rule ----
    alpaca_stocks: list[str] = field(default_factory=list)  # stocks traded on the Alpaca feed (none: crypto only; TZ_ALPACA_STOCKS=SPY,QQQ,... to add)
    # US rule: under $25,000, at most 3 day trades (buy and sell the same stock the same day) in 5 trading
    # days - Alpaca refuses the 4th (and repeat breaches lock the account for 90 days). The bots stop
    # day-trading stocks at this many, keeping the last one for your own Close all / Emergency stop.
    day_trade_guard: bool = True
    day_trades_for_bots: int = 2
    stack_coins: list[str] = field(default_factory=lambda: ["SOL", "XRP", "DOGE", "ETH", "XCN"])
    # stacked but never traded by the bots: their prices are watched for the Stacker only (Onyxcoin is small and
    # swings hard - it is held, not flipped with the trading balance). Moved out of the stack by hand, the bots
    # manage that position like any other; they just never open a new trade in it.
    stack_only_coins: list[str] = field(default_factory=lambda: ["XCN"])
    # ---- falling-market guard: no new buys while prices are clearly falling ----
    # "off" | "coin" (that coin is below its 4-hour average and the average is sloping down) |
    # "market" (most watched coins are doing that at once). Exits, stops and your own buys are never affected.
    # Tested on real price history (300 bars loaded first, as the app does), 16 windows per market: "coin" earned
    # a little more when prices rose and lost 5-10% less when they fell, but did slightly worse sideways
    # (about $2 over 16 runs) - so it is off by default. It waits for a full 4 hours of prices.
    falling_guard: str = "off"
    falling_window: int = 240  # bars (1-minute) behind the long average
    falling_slope_bars: int = 60  # how far back the average's slope is measured
    falling_min_drop: float = 0.0  # how far below its average the price must be to count (0.002 = 0.2%)
    falling_market_share: float = 0.5  # "market": the share of coins falling that pauses buying
    # ---- divide and conquer: each coin traded by its own trend (the 4-hour average above) ----
    # a coin in a clear uptrend (above its rising 4-hour average) may be bought even while the underwater cap is
    # full; a coin in a clear downtrend (below its falling average) is bought at this share of the usual size
    # (0 = not at all); everything else as usual. Off by default: 1.0 and False change nothing.
    trend_split_falling_size: float = 1.0
    trend_split_rising_past_cap: bool = False
    stack_daily_pct: float = 0.01  # budget: 1% of the trading balance a day...
    stack_daily_usd: float = 0.0  # ...or exactly this many dollars a day (0 = use the percentage)
    stack_profit_share: float = 0.25  # ...plus this share of every winning trade's profit
    stack_min_buy_usd: float = 5.0  # smallest purchase (or the venue's minimum order, if bigger)
    stack_max_buy_usd: float = 25.0  # largest single purchase
    stack_max_rsi: float = 55.0  # prefer buying a coin that isn't running hot
    stack_keep_cash: float = 0.0  # leave at least this share of the trading balance free (0: all the cash goes to crypto)
    stack_max_share: float = 0.5  # the stack never grows past this share of everything
    flip_volume_ratio: float = 2.0  # a flip needs a move on at least 2x the usual volume
    flip_min_samples: int = 12  # trades an hour needs before it's rated good or bad
    flip_good_hour_size: float = 1.25  # trades opened in a proven good hour are this much bigger...
    flip_bad_hour_size: float = 0.6  # ...and in a proven bad hour this much smaller
    # ---- accumulating: the Flipper sells a slice of a held coin into a rise and buys it back cheaper ----
    accumulate: bool = False  # off: on real price history it capped winners and paid two extra fees per round
    acc_slice: float = 0.20  # sell this share of the position...
    acc_rise_pct: float = 0.03  # ...once the price is this far above what the bots paid (and the sale is a profit)
    acc_dip_pct: float = 0.025  # buy it back once the price is this far below the sale price...
    acc_min_gain: float = 0.005  # ...and only if that gets at least 0.5% more coins than were sold, after fees
    acc_timeout_h: float = 48.0  # no dip in time: the slice simply stays sold (as profit in dollars)
    # ---- reinvest: a share of today's banked profit goes into the next trades (house money) ----
    reinvest_share: float = 0.25
    # ---- moving take-profit: once a trade is 10% past its buy price, the target moves up instead of selling ----
    tp_step: bool = True
    tp_step_pct: float = 0.10  # every +10% from the buy price: the target moves another 10% out...
    tp_step_lock: float = 0.5  # ...and the stop locks in at least half of the step reached (+5% at +10%, +10% at +20%)
    # ---- wide stop-loss: -20% from the buy price (was -33%), so ordinary dips never stop a trade out ----
    # Positions are sized so hitting it still costs about risk_per_trade of the account. The trailing stop and
    # the break-even lock still work from the normal plan's stop (-2% / -5%), so winners stay protected.
    wide_stop: bool = True
    # full cash: with the wide stop and patient exits, trades are sized (and the risk cap counted) by the patient
    # cut (-5%, meme -12%) instead of the wide stop - so the whole cash balance can be at work, not about a third of it
    full_cash: bool = True
    wide_stop_pct: float = 0.20  # (was 33%; you asked for stops at most 20% away)
    # loss floor: with never-sell-at-a-loss on, a trade this far below what was paid IS sold (0 = never).
    # Tested on real price history over 3.5-day windows: the same result when prices rise or go sideways
    # (nothing falls that far), and about a fifth less lost when they fall (-598 vs -755 / -417 vs -532).
    max_loss_pct: float = 0.20
    # ---- patient exits (real money): a wider safety stop; a losing trade is held for a comeback, and only
    #      cut early when momentum AND volume say it isn't coming back ----
    patient_exits: bool = True  # with the wide stop: cut early when momentum AND volume say no comeback
    patient_stop_pct: float = 0.05  # the safety stop for bigger coins (and stocks)...
    patient_meme_stop_pct: float = 0.12  # ...and for meme coins
    no_return_momentum: float = -0.5  # momentum this strongly against the trade...
    no_return_volume: float = 1.5  # ...on at least 1.5x the usual volume = no comeback: cut it
    patient_rescue_pct: float = 0.003  # a trade that was down and comes back to +0.3% after fees is closed
    patient_max_hours: float = 48.0  # down this long and momentum still not turning: let it go
    # ---- shorting: profit from falling prices, same stops / targets / fees, mirrored ----
    # auto = only where it really works: stocks on an Alpaca margin account that are easy to borrow
    # (practice follows your linked account; with none linked, practice shorts stocks like Alpaca would).
    # Never crypto (Kraken is used spot-only, Alpaca and Uphold can't short it) and never meme coins.
    # OFF by default: tested on real price history with stocks on Alpaca, shorts made the team lose money
    # (crypto + stocks: -$621 / -$236 with shorts vs +$171 / -$127 without, tuning / unseen half) - with
    # never-sell-at-a-loss on, a short in a rising market is held while it loses, and that loss has no ceiling.
    shorting: str = "off"  # auto | off
    # ---- growth ladder: small balances trade smaller coins; bigger coins join as the balance grows ----
    focus_small_coins: bool = True
    focus_small_until: float = 250.0  # under this: smaller coins only (meme coins and small altcoins)
    focus_mid_until: float = 1_000.0  # under this: + mid-size coins (SOL, XRP, LINK, ...); above: all coins
    # coins the bots may trade at any balance (skipping the ladder above) - and always watched. They can be
    # stacked too: the hold stack and the trading each keep their own coins.
    always_trade: list[str] = field(default_factory=lambda: ["SOL", "XRP"])
    # ---- currency pairs on Kraken (EUR/USD, GBP/USD, AUD/USD): cash against cash, Kraken's FX fee schedule
    # (0.20% a side, maker or taker). They move about a tenth as much as crypto, so they get their own target and
    # stop. Off until tested: TZ_FOREX=true, TZ_FOREX_PAIRS=EUR,GBP,AUD
    forex: bool = False
    forex_pairs: list[str] = field(default_factory=lambda: ["EUR", "GBP", "AUD"])
    forex_fee_bps: float = 20.0
    # +1.2% after fees: a target must pay 3x the round trip (0.42%), so under ~+0.85% no trade is ever taken.
    # Tested on real EUR/USD history (16 windows): +$28 / +$1 rising, -$45 / -$20 falling, -$9 / -$10 sideways -
    # the targets are rarely reached in time and the fees and held trades cost more than they make. Hence off.
    forex_target_pct: float = 0.012
    forex_stop_pct: float = 0.01
    max_small_coins: int = 8  # with Kraken prices: add up to this many small altcoins Kraken lists (if they trade enough)
    # ---- risk level for REAL money: normal | aggressive (bigger bets, wider loss limits; never leverage) ----
    risk_profile: str = "normal"
    # ---- no goal: the bots take their calculated risks every day ----
    # There used to be a daily goal ($1,000 a day) and a good-day slowdown: once a day was well up, giving some of
    # it back made new trades half size. Both are gone (you asked to remove the goal). The real risk controls stay:
    # a stop on every trade, the 20% loss floor, the underwater cap, never selling at a loss, and the losing-day
    # brake below (smaller trades on a day already down half the daily limit).
    daily_goal_usd: float = 0.0  # (0 = no daily goal)
    daily_goal_giveback: float = 0.25
    day_lock_size: float = 1.0  # (1 = a good day that slips keeps trading at full size)
    day_lock_stops: bool = False
    # ---- the goal: grow the balance rapidly with minimal losses ----
    day_lock_trigger_pct: float = 0.02  # once the day is up 2%...
    day_giveback_pct: float = 0.5  # ...giving back half of the day's best gain stops new trades until tomorrow
    losing_day_size: float = 0.5  # on a day already down half the daily limit, new trades are this size
    # ---- quick flips: on real money the bots trade more often and bank smaller wins sooner ----
    # Every target is counted AFTER fees (both buy and sell), and the hard limits above never change.
    live_quick_flips: bool = True
    flip_min_strength: float = 0.22  # conviction a real-money trade needs (practice: risk.min_signal_strength)
    reentry_bars: int = 5  # real money: a coin whose trend still holds can be traded again this many bars after its last idea
    flip_skim_rsi_max: float = 55.0  # real-money skims: a dip counts up to this RSI (practice 45)...
    flip_skim_calm: float = 0.7  # ...and the "calm enough for a tight stop" check allows a bit more movement (practice 0.5)
    flip_major_stop_pct: float = 0.02  # majors and stocks: -2% stop...
    flip_major_target_pct: float = 0.04  # ...and +4% profit after fees
    flip_meme_stop_pct: float = 0.05  # meme coins: -5% stop...
    flip_meme_target_pct: float = 0.10  # ...and +10% profit after fees
    flip_net_pct: float = 0.012  # bank a slice of a trade as soon as it is up this much after fees...
    flip_bank_fraction: float = 0.0  # ...this share (0 = off: on real price history early slices shrank the winners)
    flip_min_edge: float = 3.0  # the target must pay at least 3x the fees (you keep at least twice the fees)
    # never sell at a loss: no stop-loss or cut sells a trade below break-even after fees (it's held until it
    # recovers); the automatic loss limits stop new trades but don't sell. Your own Close all / Emergency stop still sell
    never_sell_at_loss: bool = True
    never_sell_meme_size: float = 0.5
    # Dip Buyer: buy more of a coin held at a loss once it has dipped further and turns up (lower average price)
    dip_buy: bool = True
    # ---- underwater cap: while trades held below break-even already tie up this share of the balance, no new
    # buys (and no averaging down) until some recover - so a slide can't swallow the whole balance (0 = off).
    # Tested on real price history in rising, falling and sideways markets: about half the loss when prices
    # fall, less lost sideways, but less profit when prices only rise (switch it off in Settings for that).
    loser_cap: float = 0.15
    loser_cap_depth: float = 0.005  # a trade counts once it's this far below break-even after fees (tested:
                                    # 0, 0.5%, 1%; and by price drop alone 0.5-2% - this did best on unseen data)
    # a brand-new trade starts below break-even (it hasn't earned back its fees yet): it only counts toward the
    # cap once it has been open this many minutes (0 = from the start)
    loser_cap_grace_min: float = 0.0
    # underwater budget: a new buy is made smaller so that trades below break-even - counting the new one, which
    # starts there - never tie up more than this share of the balance (0 = off; the cap above only checks what
    # is already stuck, so one big trade could land on top of it)
    underwater_budget: float = 0.0
    dip_step_pct: float = 0.06  # at least this far below the average price paid (meme coins: twice as far)...
    dip_add_fraction: float = 0.5  # ...buy this share of what's held...
    dip_max_adds: int = 2  # ...at most this many times per trade...
    dip_keep_cash: float = 0.0  # ...always leaving this share of the balance in cash (0: all the cash goes to crypto)  # ...and then a meme coin position is at most this share of the usual biggest
    profit_over_fees: float = 1.0  # a profit-taking sale keeps at least this many times the fees (after them)
    fee_rule_rescue: bool = True  # ...also for a losing trade that came back (closed only once it beats the fees)
    # ...and a stop already in profit is raised to where it would keep more than the fees. Off: on real price
    # history the tighter stops were hit more often and cost more than they kept (stops still lock break-even + fees)
    fee_rule_stops: bool = False
    strategy_exit_min_cost: float = 2.0  # a "change of mind" exit only sells a winner once it's made 2x the fees
    flip_max_hold_hours: float = 3.0  # a trade in profit after fees that's going nowhere is closed after this
    flip_meme_max_hold_hours: float = 1.5
    flip_breakeven_r: float = 1.0  # lock in break-even + fees once up 1R (sooner shook winners out on noise)
    flip_max_skims: int = 0  # real-money skims off: they lost money in every simulated run (Kraken fees vs a +1.5% target)
    flip_skim_max_minutes: float = 120.0
    flip_skim_target_pct: float = 0.015  # real-money skims aim for +1.5% after fees (practice: skim_target_pct)
    flip_skim_cooldown_min: float = 5.0
    # ---- the goal the bots work toward (by compounding - it never raises risk) ----
    goal_start: float = 50.0
    goal_target: float = 10_000.0
    # ---- Grok (xAI) AI analyst ----
    # ---- Coach: the team learns from its own closed trades (hard safety limits never change) ----
    coach_learning: bool = True
    learn_targets: bool = True  # Target Learner: each coin's take-profit from how far its winning trades really ran
    learn_targets_raise_only: bool = True  # ...only ever raising a target (lowering them sold winners too soon)
    learn_sources: bool = True  # Trade Reviewer: size each trade finder by its record
    coach_bench_minutes: float = 45.0  # how long a coin that keeps losing sits out
    coach_idle_hours: float = 6.0  # idle this long with healthy results -> lower the bar a little
    grok_api_key: str = ""
    grok_model: str = "grok-4"
    grok_base_url: str = "https://api.x.ai/v1"
    grok_mode: str = "auto"  # auto (veto if key set) | veto | advisory | off
    grok_timeout_s: float = 20.0
    grok_max_signal_age_s: float = 30.0
    grok_fail_open: bool = False  # if Grok is unreachable: False = reject entries (safer)
    # ---- execution accuracy ----
    max_slippage_bps: float = 50.0  # fills worse than this vs. reference price -> auto-pause
    # ---- wallets / funding ----
    deposit_addresses: list[str] = field(default_factory=list)  # "CHAIN:address", e.g. XRP:rXXXX
    withdrawal_cooldown_h: float = 24.0  # newly linked wallets can't receive withdrawals until then
    max_daily_withdrawal: float = 10_000.0
    test_send_usd: float = 25.0  # first send to a new wallet is capped at this until you confirm it arrived
    # ---- Executive (head trader) ----
    exec_max_chase_r: float = 0.5  # hold an entry if price already moved this many R past the plan
    exec_stale_after_s: float = 20.0  # hold entries when a symbol's price is older than this
    exec_max_correlated: int = 20  # max same-direction crypto positions at once (the Correlation Officer still
    # blocks a third copy of the very same move)
    exec_lookback_trades: int = 20
    exec_market_hours_only: bool = True  # stocks: only open positions during regular US market hours
    exec_defensive_after_losses: int = 3  # losses in a row that switch to the defensive stance
    exec_defensive_min_strength: float = 0.45  # conviction needed while defensive, at most...
    exec_defensive_extra: float = 0.10  # ...or the usual bar + this, whichever is lower (real money: 0.22 + 0.10)
    exec_defensive_cooloff_min: float = 20.0  # a losing streak stops counting this long after its last loss
    # ---- autonomy ----
    auto_rearm_daily: bool = True  # a daily-loss kill switch re-arms itself at the next UTC day
    resume_on_restart: bool = True  # restore books, stops and mode (incl. live) after a restart
    # ---- stop-loss placement (see core/stops.py) ----
    # percent (default): fixed % stop and target from the entry price - meme coins use
    # stop_loss_pct / take_profit_pct, the bigger, calmer coins (BTC, ETH, ...) use the major_* pair,
    # sized so a trade can actually reach its target in hours rather than days.
    # structure: beyond the recent swing low/high. atr: a fixed ATR multiple.
    stop_method: str = "percent"
    stop_loss_pct: float = 0.08  # meme coins: -8% stop-loss
    take_profit_pct: float = 0.15  # meme coins: +15% take-profit
    major_stop_loss_pct: float = 0.03  # other coins and stocks: -3%
    major_take_profit_pct: float = 0.06  # other coins and stocks: +6%
    # Runners: at the take-profit, sell runner_sell_fraction and let the rest run. Its stop moves
    # into profit (at least runner_lock_share of the target) and then trails the peak.
    runners: bool = True
    runner_sell_fraction: float = 0.33  # at the target bank a third; two thirds ride with no ceiling
    runner_lock_share: float = 0.5
    # Quick flips: a trade that went nowhere for this long, but is in profit after fees, is closed
    # to free the slot for a better chance (losers are still left to their stop).
    max_hold_hours: float = 24.0
    meme_max_hold_hours: float = 6.0
    # ---- Reporter: world & crypto news (public RSS feeds + Fear & Greed index, no account) ----
    news: bool = True
    news_alpaca: bool = True  # Alpaca accounts: its news service too (headlines tagged with their stocks / coins)
    news_feeds: list[str] = field(default_factory=list)  # empty = built-in list (CoinDesk, Cointelegraph, BBC, ...)
    # ---- Skimmer: quick day-trade skims + profit-taking on every open trade ----
    skimmer: bool = True
    skim_target_pct: float = 0.025  # skims aim for +2.5% (after ~0.6% fees a win still beats a loss)...
    skim_stop_pct: float = 0.012  # ...with a -1.2% stop
    skim_max_minutes: float = 120.0  # day trade: sold when time is up, if in profit after fees
    max_skims: int = 2  # skims open at once
    skim_cooldown_min: float = 15.0  # wait this long before skimming the same coin again
    bank_at_share: float = 0.5  # bank a slice once a trade is this share of the way to its target
    bank_fraction: float = 0.25  # ...this much of it
    spike_bars: int = 5  # a jump within this many bars (5 min on 1-min bars) counts as a spike
    spike_pct: float = 0.025  # majors: +2.5% within the window
    meme_spike_pct: float = 0.06  # meme coins: +6%
    spike_fraction: float = 0.5  # sell this much into a spike
    # ---- crypto focus / meme coins ----
    meme_coins: list[str] = field(default_factory=lambda: list(MEME_COINS))
    meme_risk_mult: float = 0.5  # meme coins risk this fraction of a normal trade (they swing hard)
    max_meme_positions: int = 5  # meme coins held at once (+1 Aggressive, +2 Bold)
    # Sniping: on a trusted meme coin, a live price breaking this far above its recent high is
    # acted on at once (not at the bar close), if momentum agrees.
    # strategies the Quant runs beyond momentum / mean reversion / breakout. All ten exist (Settings -> Trading
    # strategies); on by default are the two that didn't cost profit on real price history
    strategies: list[str] = field(default_factory=lambda: ["macd", "obv"])
    # dip buys led by mean reversion (RSI under 35): tested on real price history they went bad MORE often than
    # buying at random, in rising, falling and sideways markets alike - they buy while the coin is still falling.
    # "off" (as before) | "turn" (wait until the RSI and the price tick back up) | "trend" (only above the
    # 50-bar average) | "both"; and the share of the vote mean reversion gets in a sideways market (was 0.7)
    mr_buy_filter: str = "off"
    mr_ranging_weight: float = 0.7
    pullback: bool = True  # the Pullback Hunter: buy the retest of a breakout
    ema_fast_period: int = 12  # the fast and slow moving averages behind momentum and MACD
    ema_slow_period: int = 26
    snipe: bool = True
    # volume snipes: any crypto or meme coin whose bar trades several times its usual volume while the price
    # jumps up is bought on that bar (then the usual checks: Risk, the advisors, fees, the stop)
    surge_snipe: bool = True
    # ---- volume breakout: the price beats its highest close of the last vb_high_bars on at least vb_volume_ratio
    # times its average volume of the last vb_volume_bars. Found by scoring candidate buy signals against random
    # entries on real price history: the only one ahead of random in rising, falling and sideways markets alike,
    # on both halves of the data, and its neighbours (3-6 h, 1.2-1.5x) were too. Off until the full replay says so.
    vol_breakout: bool = False
    vb_high_bars: int = 240
    vb_volume_bars: int = 60
    vb_volume_ratio: float = 1.3
    vb_cooldown_bars: int = 30
    surge_volume_ratio: float = 4.0  # this many times the usual volume (3x and 2.5x tested worse on unseen data)...
    surge_min_move: float = 0.003  # ...and the bar up at least this much
    surge_max_rsi: float = 80.0  # not when it's already wildly overbought
    surge_cooldown_bars: int = 30  # one snipe per coin per surge
    snipe_breakout_pct: float = 0.01
    stop_buffer_atr: float = 1.0  # buffer beyond the swing level
    min_stop_atr: float = 1.5  # never tighter than this (noise)
    max_stop_atr: float = 4.0  # skip the trade if the structural stop is wider than this
    reward_risk: float = 2.0  # target = this x stop distance
    min_room_r: float = 1.0  # need this much room (in R) before the nearest resistance/support
    stop_atr_mult: float = 3.0  # used when stop_method=atr
    take_profit_atr_mult: float = 6.0
    trailing_stop: bool = True
    # ---- profit protection: a trade that has gone green must not end red ----
    # Once open profit reaches this multiple of the initial risk (entry-to-stop distance), the stop
    # moves to break-even plus round-trip costs, and then locks in profit_lock_pct of peak profit.
    breakeven_trigger_r: float = 0.5
    profit_lock_pct: float = 0.5
    scale_out_r: float = 1.0  # at +1R, take partial profit...
    scale_out_fraction: float = 0.5  # ...on this share of the position (0 disables)
    exit_only_in_profit: bool = True  # strategy-driven exits only when net profitable; stops still cut losers
    risk: RiskLimits = field(default_factory=RiskLimits)

    def aggressive(self, live: bool) -> bool:
        """Real money on a level above Normal (Aggressive or Bold)."""
        return live and self.risk_profile in PROFILES

    def boost(self, live: bool) -> dict | None:
        """The numbers of the real-money level above Normal in force, or None."""
        return PROFILES[self.risk_profile] if self.aggressive(live) else None

    def limits(self, live: bool) -> RiskLimits:
        """The risk limits in force: Aggressive (real money only) roughly doubles what a trade may risk and
        widens the loss limits. Stops stay on every trade, and nothing is ever borrowed (no leverage)."""
        if not live:
            return self.risk
        if not self.aggressive(live):
            return replace(self.risk, risk_per_trade=self.live_risk_per_trade, max_position_pct=self.live_max_position_pct,
                           max_portfolio_heat=self.live_max_portfolio_heat,
                           max_daily_loss_pct=max(self.risk.max_daily_loss_pct, self.live_max_daily_loss_pct),
                           max_drawdown_pct=max(self.risk.max_drawdown_pct, self.live_max_drawdown_pct))
        b = PROFILES[self.risk_profile]
        return replace(self.risk, risk_per_trade=b["risk_per_trade"], max_position_pct=b["max_position_pct"],
                       max_open_positions=max(self.risk.max_open_positions, b["max_open_positions"]),
                       max_portfolio_heat=b["max_portfolio_heat"],
                       max_daily_loss_pct=max(self.risk.max_daily_loss_pct, b["daily_loss"]),
                       max_drawdown_pct=max(self.risk.max_drawdown_pct, b["drawdown"]))

    def live_limits(self) -> tuple[float, float, float]:
        """(daily loss, drawdown, capital floor) that stop real-money trading."""
        if self.risk_profile in PROFILES:
            b = PROFILES[self.risk_profile]
            return b["daily_loss"], b["drawdown"], b["floor"]
        return self.live_max_daily_loss_pct, self.live_max_drawdown_pct, self.live_capital_floor_pct

    def patient(self, live: bool) -> bool:
        """Patient exits: real money with patient_exits on - and practice too when the wide stop is on, so a falling
        trade there is also cut early (on momentum + volume) instead of riding to the wide stop: practice previews real money."""
        return self.patient_exits and (live or self.wide_stop)

    def sizing_stop_pct(self, symbol: str, live: bool) -> float | None:
        """The loss a trade is sized for when the whole cash balance may trade (None = size by the stop itself)."""
        if not (self.full_cash and self.wide_stop and self.patient(live)):
            return None
        return self.patient_meme_stop_pct if self.is_meme(symbol) else self.patient_stop_pct

    def quick(self, live: bool) -> bool:
        """Quick-flip mode: real money with live_quick_flips on."""
        return live and self.live_quick_flips

    def coin_size(self, symbol: str) -> str:
        """large | mid | small (stocks count as large: they're not part of the ladder)."""
        if not self.is_crypto(symbol):
            return "large"
        coin = symbol.upper().replace("/", "-").split("-")[0]
        return "large" if coin in LARGE_COINS else "mid" if coin in MID_COINS else "small"

    def plan_pcts(self, symbol: str, quick: bool = False) -> tuple[float, float]:
        """(stop-loss %, take-profit %) for a symbol under the percent plan (the target is after fees)."""
        if self.is_forex(symbol):
            return self.forex_stop_pct, self.forex_target_pct
        if self.is_meme(symbol):
            return (self.flip_meme_stop_pct, self.flip_meme_target_pct) if quick else (self.stop_loss_pct, self.take_profit_pct)
        if quick:
            return self.flip_major_stop_pct, self.flip_major_target_pct
        return self.major_stop_loss_pct, self.major_take_profit_pct

    def is_stack_only(self, symbol: str) -> bool:
        """A coin the Stacker buys and the trading bots never open a trade in."""
        return symbol.upper().replace("/", "-").split("-")[0] in self.stack_only_coins

    def is_meme(self, symbol: str) -> bool:
        return self.is_crypto(symbol) and symbol.upper().replace("/", "-").split("-")[0] in self.meme_coins

    @staticmethod
    def live_tradeable(symbol: str) -> bool:
        """Whether the real-money broker (Alpaca) can trade this symbol."""
        if not ("-" in symbol or "/" in symbol):
            return True  # US stocks
        return symbol.upper().replace("/", "-").split("-")[0] in ALPACA_CRYPTO

    @staticmethod
    def is_crypto(symbol: str) -> bool:
        return "-" in symbol or "/" in symbol

    @staticmethod
    def is_forex(symbol: str) -> bool:
        """A currency pair (EUR-USD, ...): cash against cash."""
        base, _, quote = symbol.upper().replace("/", "-").partition("-")
        return quote == "USD" and base in FOREX

    def fee_bps(self, symbol: str) -> float:
        if self.is_forex(symbol):
            return self.forex_fee_bps
        return self.crypto_fee_bps if self.is_crypto(symbol) else self.stock_fee_bps

    def round_trip_cost(self, symbol: str, price: float) -> float:
        """Per-unit cost to enter and exit: fees + slippage both ways, plus a small margin."""
        return price * (2 * (self.fee_bps(symbol) + self.slippage_bps) + 2) / 10_000

    def net_if_sold(self, pos, price: float) -> float:
        """What selling the whole position at ``price`` would really make: price move minus the buy fee
        already paid and the sell fee + slippage still to come (with a small margin)."""
        qty = abs(pos.qty)
        paid = pos.entry_fees if getattr(pos, "entry_fees", 0.0) > 0 else qty * pos.avg_price * self.fee_bps(pos.symbol) / 10_000
        exit_cost = qty * price * (self.fee_bps(pos.symbol) + self.slippage_bps + 1) / 10_000
        return (price - pos.avg_price) * pos.qty - paid - exit_cost

    def fees_if_sold(self, pos, price: float) -> float:
        """The fees one round trip of this position costs: the buy fee paid plus the sell fee to come."""
        qty = abs(pos.qty)
        paid = pos.entry_fees if getattr(pos, "entry_fees", 0.0) > 0 else qty * pos.avg_price * self.fee_bps(pos.symbol) / 10_000
        return paid + qty * price * self.fee_bps(pos.symbol) / 10_000

    def beats_fees(self, pos, price: float) -> bool:
        """Selling now keeps more profit than the trade paid in fees (the rule for every profit-taking sale)."""
        return self.net_if_sold(pos, price) >= self.profit_over_fees * self.fees_if_sold(pos, price)

    def venue(self) -> str:
        """The real-money venue in use: uphold, kraken or alpaca."""
        if self.live_venue in VENUES:
            return self.live_venue
        if self.uphold_token:
            return "uphold"
        return "kraken" if self.kraken_key else "alpaca"

    def apply_venue_costs(self) -> None:
        """Practice with the real venue's costs (Uphold's spread ~1%, Kraken's fee 0.40%, Alpaca's 0.25%).

        Re-run whenever the venue changes: switching to a cheaper venue restores the normal fee."""
        if not hasattr(self, "_base_crypto_fee_bps"):
            self._base_crypto_fee_bps = self.crypto_fee_bps
        base = self._base_crypto_fee_bps
        venue_fee = {"uphold": self.uphold_fee_bps, "kraken": self.kraken_fee_bps}.get(self.venue())
        if venue_fee is not None and "TZ_CRYPTO_FEE_BPS" not in os.environ:
            self.crypto_fee_bps = max(base, venue_fee)
        else:
            self.crypto_fee_bps = base

    def data_keys(self) -> tuple[str, str] | None:
        if self.alpaca_key and self.alpaca_secret:
            return self.alpaca_key, self.alpaca_secret
        if self.alpaca_live_key and self.alpaca_live_secret:
            return self.alpaca_live_key, self.alpaca_live_secret
        return None

    def live_blockers(self) -> list[str]:
        """Configuration problems preventing live trading (empty = live can be enabled)."""
        out = []
        if self.venue() == "uphold":
            if not self.uphold_token:
                out.append("connect your Uphold account first: Money → Real-money account "
                           "(server setting TZ_UPHOLD_TOKEN)")
        elif self.venue() == "kraken":
            if not (self.kraken_key and self.kraken_secret):
                out.append("connect your Kraken account first: Money → Real-money account "
                           "(server setting TZ_KRAKEN_KEY / TZ_KRAKEN_SECRET)")
        elif not (self.alpaca_live_key and self.alpaca_live_secret):
            out.append("connect your real-money account first: Money → Real-money account "
                       "(server setting TZ_ALPACA_LIVE_KEY / TZ_ALPACA_LIVE_SECRET)")
        if self.feed == "sim":
            out.append("real money needs live prices, but this server uses simulated practice prices (TZ_FEED=sim)")
        return out

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        for f in fields(s):
            if f.name == "risk":
                continue
            setattr(s, f.name, _env(f.name, getattr(s, f.name)))
        for f in fields(s.risk):
            setattr(s.risk, f.name, _env(f.name, getattr(s.risk, f.name)))
        return s

    def validate(self) -> None:
        if not self.journal_path:  # keep the PIN, trades and learning on the persistent disk if there is one
            self.journal_path = "/data/tradezone.db" if os.path.isdir("/data") and os.access("/data", os.W_OK) \
                else "tradezone.db"
        if self.mode not in ("paper", "live"):
            raise ValueError("TZ_MODE must be 'paper' or 'live'")
        if self.feed not in ("auto", "sim", "coinbase", "kraken", "alpaca"):
            raise ValueError("TZ_FEED must be auto, coinbase, kraken, alpaca or sim")
        if not self.feed_choice:
            self.feed_choice = self.feed
        if self.feed == "auto":
            self.feed = "kraken" if self.venue() == "kraken" else "alpaca" if self.data_keys() else "coinbase"
        if not self.symbols:
            self.symbols = list({"coinbase": CRYPTO_SYMBOLS, "kraken": CRYPTO_SYMBOLS,
                                 "alpaca": [*ALPACA_SYMBOLS, *self.alpaca_stocks]}.get(self.feed, MIXED_SYMBOLS))
        self.stack_coins = [c.strip().upper().split("-")[0] for c in self.stack_coins if c.strip()]
        self.always_trade = [c.strip().upper().split("-")[0] for c in self.always_trade if c.strip()]
        self.forex_pairs = [c.strip().upper().split("-")[0] for c in self.forex_pairs if c.strip().upper().split("-")[0] in FOREX]
        if self.forex and self.feed == "kraken":  # (only Kraken quotes and trades them)
            for c in self.forex_pairs:
                if f"{c}-USD" not in self.symbols:
                    self.symbols.append(f"{c}-USD")
        if self.feed in ("coinbase", "kraken"):  # always-traded coins are always watched
            for c in self.always_trade:
                if f"{c}-USD" not in self.symbols:
                    self.symbols.append(f"{c}-USD")
        self.stack_only_coins = [c.strip().upper().split("-")[0] for c in self.stack_only_coins if c.strip()]
        if self.stack and self.feed in ("coinbase", "kraken"):  # watch a stack-only coin's price (the feed skips it if unlisted)
            for c in self.stack_only_coins:
                if c in self.stack_coins and f"{c}-USD" not in self.symbols:
                    self.symbols.append(f"{c}-USD")
        if self.feed in ("coinbase", "kraken"):
            bad = [s for s in self.symbols if not s.endswith("-USD")]
            if bad:
                raise ValueError(f"{self.feed.title()} only quotes crypto pairs like BTC-USD; not: {', '.join(bad)}")
        if self.live_venue not in ("auto", *VENUES):
            raise ValueError("TZ_LIVE_VENUE must be auto, uphold, kraken or alpaca")
        self.uphold_token = self.uphold_token.strip()
        self.apply_venue_costs()
        if self.broker not in ("paper", "alpaca"):
            raise ValueError("TZ_BROKER must be paper or alpaca")
        if self.mode == "live":
            if self.live_confirm != LIVE_CONFIRM_PHRASE:
                raise ValueError(
                    f"Starting directly in live mode requires TZ_LIVE_CONFIRM='{LIVE_CONFIRM_PHRASE}' "
                    "(or start in paper and use the dashboard's Go LIVE button)"
                )
            if self.live_blockers():
                raise ValueError("Live mode unavailable: " + "; ".join(self.live_blockers()))
        if self.broker == "alpaca" and not (self.alpaca_key and self.alpaca_secret):
            raise ValueError("TZ_BROKER=alpaca requires TZ_ALPACA_KEY and TZ_ALPACA_SECRET (paper account)")
        if self.feed == "alpaca" and not self.data_keys():
            raise ValueError("TZ_FEED=alpaca requires Alpaca API keys")
        if self.risk_profile not in ("normal", *PROFILES):
            raise ValueError("TZ_RISK_PROFILE must be normal, aggressive or bold")
        if self.shorting not in ("auto", "off"):
            raise ValueError("TZ_SHORTING must be auto or off")
        if self.stop_method not in ("structure", "atr", "percent"):
            raise ValueError("TZ_STOP_METHOD must be percent, structure or atr")
        for lo, hi in ((self.stop_loss_pct, self.take_profit_pct), (self.major_stop_loss_pct, self.major_take_profit_pct)):
            if not (0 < lo < 0.5 and 0 < hi < 5):
                raise ValueError("stop-loss % must be between 0 and 0.5 and take-profit % between 0 and 5")
        if not 0 < self.runner_sell_fraction <= 1:
            raise ValueError("TZ_RUNNER_SELL_FRACTION must be above 0 and at most 1")
        self.meme_coins = [c.upper().split("-")[0] for c in self.meme_coins]
        if self.live_max_capital < 0:
            raise ValueError("TZ_LIVE_MAX_CAPITAL must be >= 0")
        if not self.grok_api_key:
            self.grok_api_key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY") or ""
        self.grok_api_key = self.grok_api_key.strip()
        if self.grok_mode not in ("auto", "veto", "advisory", "off"):
            raise ValueError("TZ_GROK_MODE must be auto, veto, advisory or off")
        if self.grok_mode == "auto":
            self.grok_mode = "veto" if self.grok_api_key else "off"
        if not self.symbols:
            raise ValueError("TZ_SYMBOLS must list at least one symbol")
        if self.cycle_seconds <= 0:
            raise ValueError("TZ_CYCLE_SECONDS must be positive")
