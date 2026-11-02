"""Runtime configuration, loaded from environment variables (prefix ``TZ_``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

LIVE_CONFIRM_PHRASE = "I UNDERSTAND THE RISKS"
#: what the operator types in the dashboard to switch to live trading
LIVE_SWITCH_WORD = "LIVE"


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


@dataclass
class RiskLimits:
    risk_per_trade: float = 0.01  # fraction of equity risked to the stop
    max_position_pct: float = 0.20  # max notional per symbol as fraction of equity
    max_gross_exposure: float = 1.0  # max total notional as multiple of equity
    max_open_positions: int = 5
    max_daily_loss_pct: float = 0.05  # kill switch trips beyond this daily loss
    max_drawdown_pct: float = 0.15  # kill switch trips beyond this drawdown from peak
    max_orders_per_minute: int = 30
    min_signal_strength: float = 0.35
    symbol_cooldown_s: float = 10.0
    allow_short: bool = False
    fractional: bool = True


@dataclass
class Settings:
    # Startup mode. The desk always starts in paper unless this is "live" AND TZ_LIVE_CONFIRM is set.
    # Day to day, switch with the dashboard's Go LIVE / Back to PAPER button instead.
    mode: str = "paper"  # paper | live
    feed: str = "sim"  # sim | coinbase | alpaca
    broker: str = "paper"  # broker used in PAPER mode: paper (internal simulator) | alpaca (Alpaca paper account)
    symbols: list[str] = field(
        default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD", "AAPL", "NVDA", "TSLA"]
    )
    starting_cash: float = 100_000.0
    cycle_seconds: float = 1.0
    commission_bps: float = 1.0
    slippage_bps: float = 2.0
    seed: int = 7
    journal_path: str = "tradezone.db"
    host: str = "127.0.0.1"
    port: int = 8000
    api_token: str = ""  # if set, control endpoints require "Authorization: Bearer <token>"
    live_confirm: str = ""
    alpaca_key: str = ""  # Alpaca PAPER account keys (paper broker / market data)
    alpaca_secret: str = ""
    alpaca_live_key: str = ""  # Alpaca LIVE (real money) account keys - enables the Go LIVE button
    alpaca_live_secret: str = ""
    live_max_capital: float = 0.0  # if > 0, live mode trades at most this much USD of the account
    # ---- Grok (xAI) AI analyst ----
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
    stop_atr_mult: float = 3.0
    take_profit_atr_mult: float = 6.0
    trailing_stop: bool = True
    risk: RiskLimits = field(default_factory=RiskLimits)

    def data_keys(self) -> tuple[str, str] | None:
        if self.alpaca_key and self.alpaca_secret:
            return self.alpaca_key, self.alpaca_secret
        if self.alpaca_live_key and self.alpaca_live_secret:
            return self.alpaca_live_key, self.alpaca_live_secret
        return None

    def live_blockers(self) -> list[str]:
        """Configuration problems preventing live trading (empty = live can be enabled)."""
        out = []
        if not (self.alpaca_live_key and self.alpaca_live_secret):
            out.append("set TZ_ALPACA_LIVE_KEY and TZ_ALPACA_LIVE_SECRET (your Alpaca live-account keys)")
        if self.feed == "sim":
            out.append("live orders can't be priced off simulated data - set TZ_FEED=alpaca or coinbase")
        return out

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        for f in fields(s):
            if f.name == "risk":
                continue
            setattr(s, f.name, _env(f.name, getattr(s, f.name)))
        for f in fields(s.risk):
            setattr(s.risk, f.name, _env(f.name, getattr(s.risk, f.name)))
        return s

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("TZ_MODE must be 'paper' or 'live'")
        if self.feed not in ("sim", "coinbase", "alpaca"):
            raise ValueError("TZ_FEED must be sim, coinbase or alpaca")
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
        if self.live_max_capital < 0:
            raise ValueError("TZ_LIVE_MAX_CAPITAL must be >= 0")
        if not self.grok_api_key:
            self.grok_api_key = os.environ.get("XAI_API_KEY", "")
        if self.grok_mode not in ("auto", "veto", "advisory", "off"):
            raise ValueError("TZ_GROK_MODE must be auto, veto, advisory or off")
        if self.grok_mode == "auto":
            self.grok_mode = "veto" if self.grok_api_key else "off"
        if not self.symbols:
            raise ValueError("TZ_SYMBOLS must list at least one symbol")
        if self.cycle_seconds <= 0:
            raise ValueError("TZ_CYCLE_SECONDS must be positive")
