"""Position ledger with average-cost accounting and mark-to-market."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Fill, Model, now

EPS = 1e-9


@dataclass
class Position(Model):
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    last_price: float = 0.0
    stop_price: float | None = None
    take_profit: float | None = None
    high_water: float | None = None  # best price since entry, for trailing stops
    opened_ts: float | None = None
    entry_fees: float = 0.0  # buy fees paid on what's still held (charged to the trade when it's sold)

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_price) * self.qty

    @property
    def is_flat(self) -> bool:
        return abs(self.qty) < EPS

    def to_dict(self):  # type: ignore[override]
        d = super().to_dict()
        d["market_value"] = self.market_value
        d["unrealized_pnl"] = self.unrealized_pnl
        return d


@dataclass
class ClosedTrade(Model):
    symbol: str
    side: str  # side of the position that was closed (long/short)
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    ts: float = field(default_factory=now)


class Portfolio:
    KEEP_TRADES = 10_000  # closed trades kept in memory for the stats
    def __init__(self, starting_cash: float) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.commissions = 0.0
        self.peak_equity = starting_cash
        self.day_start_equity = starting_cash
        self.net_flows = 0.0  # deposits - withdrawals

    def apply_flow(self, amount: float) -> None:
        """Deposit (+) or withdraw (-) cash. Baselines move too, so funding isn't counted as P&L."""
        self.cash += amount
        self.net_flows += amount
        self.starting_cash += amount
        self.peak_equity += amount
        self.day_start_equity += amount

    def position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol)
        return self.positions[symbol]

    def mark(self, symbol: str, price: float) -> None:
        pos = self.position(symbol)
        pos.last_price = price

    def apply_fill(self, fill: Fill) -> ClosedTrade | None:
        pos = self.position(fill.symbol)
        signed = fill.qty * fill.side.sign
        self.cash -= signed * fill.price + fill.commission
        self.commissions += fill.commission
        pos.last_price = fill.price or pos.last_price
        closed: ClosedTrade | None = None

        if pos.is_flat or (pos.qty > 0) == (signed > 0):
            # opening or adding: blend average cost
            new_qty = pos.qty + signed
            pos.avg_price = (pos.avg_price * pos.qty + fill.price * signed) / new_qty
            pos.entry_fees += fill.commission
            if pos.is_flat:
                pos.opened_ts = fill.ts
                pos.high_water = fill.price
            pos.qty = new_qty
        else:
            # reducing, closing or flipping
            close_qty = min(abs(signed), abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            share = close_qty / abs(pos.qty) if abs(pos.qty) > EPS else 1.0
            entry_fee = pos.entry_fees * min(share, 1.0)
            pos.entry_fees -= entry_fee
            pnl = (fill.price - pos.avg_price) * close_qty * direction - fill.commission * close_qty / abs(signed) - entry_fee
            pos.realized_pnl += pnl
            closed = ClosedTrade(
                fill.symbol,
                "long" if direction > 0 else "short",
                close_qty,
                pos.avg_price,
                fill.price,
                pnl,
            )
            self.closed_trades.append(closed)
            if len(self.closed_trades) > self.KEEP_TRADES:  # memory stays flat (the journal keeps every trade)
                del self.closed_trades[: len(self.closed_trades) - self.KEEP_TRADES]
            remainder = abs(signed) - close_qty
            if remainder > EPS:  # flipped through zero
                pos.qty = remainder * (1 if signed > 0 else -1)
                pos.avg_price = fill.price
                pos.entry_fees = fill.commission * remainder / abs(signed)
                pos.opened_ts = fill.ts
                pos.high_water = fill.price
                pos.stop_price = pos.take_profit = None
            else:
                pos.qty += signed
                if pos.is_flat:
                    pos.qty = 0.0
                    pos.avg_price = pos.entry_fees = 0.0
                    pos.stop_price = pos.take_profit = pos.high_water = None
                    pos.opened_ts = None
        return closed

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat]

    @property
    def equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions.values())

    @property
    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def update_watermarks(self) -> None:
        self.peak_equity = max(self.peak_equity, self.equity)

    @property
    def drawdown(self) -> float:
        return 0.0 if self.peak_equity <= 0 else 1 - self.equity / self.peak_equity

    @property
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.equity / self.day_start_equity - 1

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "equity": self.equity,
            "starting_cash": self.starting_cash,
            "gross_exposure": self.gross_exposure,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.equity - self.starting_cash,
            "return_pct": self.equity / self.starting_cash - 1 if self.starting_cash else 0.0,
            "commissions": self.commissions,
            "net_flows": self.net_flows,
            "drawdown": self.drawdown,
            "peak_equity": self.peak_equity,
            "daily_pnl_pct": self.daily_pnl_pct,
            "positions": [p.to_dict() for p in self.open_positions],
            "closed_trades": len(self.closed_trades),
        }
