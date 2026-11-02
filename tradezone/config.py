"""Runtime configuration, loaded from environment variables (prefix ``TZ_``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

LIVE_CONFIRM_PHRASE = "I UNDERSTAND THE RISKS"


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
    mode: str = "paper"  # paper | live
    feed: str = "sim"  # sim | coinbase | alpaca
    broker: str = "paper"  # paper | alpaca
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
    alpaca_key: str = ""
    alpaca_secret: str = ""
    alpaca_paper: bool = True
    stop_atr_mult: float = 3.0
    take_profit_atr_mult: float = 6.0
    trailing_stop: bool = True
    risk: RiskLimits = field(default_factory=RiskLimits)

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
                    f"Live trading requires TZ_LIVE_CONFIRM='{LIVE_CONFIRM_PHRASE}'"
                )
            if self.broker == "paper":
                raise ValueError("Live mode needs a real broker (TZ_BROKER=alpaca)")
            if self.feed == "sim":
                raise ValueError("Live mode cannot trade on simulated prices (set TZ_FEED)")
        if self.broker == "alpaca" or self.feed == "alpaca":
            if not (self.alpaca_key and self.alpaca_secret):
                raise ValueError("Alpaca requires TZ_ALPACA_KEY and TZ_ALPACA_SECRET")
        if self.broker == "alpaca" and self.mode == "paper" and not self.alpaca_paper:
            raise ValueError("TZ_MODE=paper with TZ_ALPACA_PAPER=false would trade real money")
        if self.mode == "live" and self.broker == "alpaca" and self.alpaca_paper:
            # live mode explicitly targets the real-money endpoint
            self.alpaca_paper = False
        if not self.symbols:
            raise ValueError("TZ_SYMBOLS must list at least one symbol")
        if self.cycle_seconds <= 0:
            raise ValueError("TZ_CYCLE_SECONDS must be positive")
