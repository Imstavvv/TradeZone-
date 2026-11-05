"""Shared domain models passed between bots over the event bus."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class SignalKind(str, Enum):
    ENTRY = "entry"  # open / flip a position (subject to full risk checks)
    EXIT = "exit"  # reduce-only close (always allowed, even when halted)


class OrderStatus(str, Enum):
    NEW = "new"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)


class Regime(str, Enum):
    UNKNOWN = "unknown"
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


_ids = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}-{next(_ids):06d}"


def now() -> float:
    return time.time()


def _conv(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:  # the common case first
        return v.value if isinstance(v, Enum) else v
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, Model):
        return v.to_dict()
    if isinstance(v, dict):
        return {k: _conv(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_conv(x) for x in v]
    return v


class Model:
    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict. Reads the fields directly (no deep copy like dataclasses.asdict),
        which matters because the live page converts ~100 orders/trades every second."""
        names = type(self).__dict__.get("_field_names")
        if names is None:
            names = tuple(f.name for f in fields(self))  # type: ignore[arg-type]
            type(self)._field_names = names  # type: ignore[attr-defined]
        return {k: _conv(getattr(self, k)) for k in names}


@dataclass
class Tick(Model):
    symbol: str
    price: float
    volume: float = 0.0
    ts: float = field(default_factory=now)


@dataclass
class Snapshot(Model):
    """Market Scout output: price plus computed indicators."""

    symbol: str
    price: float
    indicators: dict[str, float]
    ts: float = field(default_factory=now)
    historical: bool = False  # built from back-filled candles: update state, never trade on it


@dataclass
class RegimeUpdate(Model):
    symbol: str
    regime: Regime
    confidence: float
    metrics: dict[str, float] = field(default_factory=dict)
    ts: float = field(default_factory=now)


@dataclass
class Signal(Model):
    symbol: str
    side: Side
    strength: float  # 0..1
    reason: str
    source: str
    kind: SignalKind = SignalKind.ENTRY
    fraction: float = 1.0  # exits: share of the position to close (partial take-profit)
    calm: bool = False  # exits: taking profit, no hurry - may wait at the ask for the lower maker fee
    votes: dict[str, str] = field(default_factory=dict)  # bot/strategy -> yes | no | abstain
    qty: float | None = None  # an exact amount (the Flipper's buy-backs add this many coins to a held position)
    id: str = field(default_factory=lambda: new_id("sig"))
    ts: float = field(default_factory=now)


@dataclass
class OrderRequest(Model):
    symbol: str
    side: Side
    qty: float
    reason: str
    signal_id: str
    reduce_only: bool = False
    stop_price: float | None = None
    take_profit: float | None = None
    ref_price: float | None = None  # market price when Risk approved (for the no-chase check)
    calm: bool = False  # a profit-taking exit that may wait at the ask (maker fee); stops never do
    id: str = field(default_factory=lambda: new_id("req"))
    ts: float = field(default_factory=now)


@dataclass
class Order(Model):
    symbol: str
    side: Side
    qty: float
    request_id: str
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    broker_id: str | None = None
    reason: str = ""
    reduce_only: bool = False
    stop_price: float | None = None
    take_profit: float | None = None
    maker: bool = False  # entry placed as a post-only limit at the bid / ask (lower fee); market for any rest
    limit_price: float | None = None
    id: str = field(default_factory=lambda: new_id("ord"))
    ts: float = field(default_factory=now)


@dataclass
class Fill(Model):
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    commission: float = 0.0
    id: str = field(default_factory=lambda: new_id("fill"))
    ts: float = field(default_factory=now)


@dataclass
class Alert(Model):
    level: str  # info | warning | critical
    message: str
    source: str
    ts: float = field(default_factory=now)
