"""Kraken spot trading adapter (real money) - trades only, it can never withdraw.

Safety, in three layers:

1. **This code can't withdraw.** Every private Kraken call goes through ``_private``, which only
   allows a fixed list of trading / balance methods. Anything that moves money off the exchange
   (``Withdraw``, ``WalletTransfer``, earn / staking, ...) is refused here before a request is made.
2. **The key can't withdraw.** When you connect, TradeZone checks the key with a harmless
   ``WithdrawInfo`` request (it only reads withdrawal *info*, it never withdraws). If Kraken answers
   anything other than "Permission denied", the key has withdraw rights and is **refused**.
3. **Your own coins are left alone.** When live trading starts, TradeZone notes what you already
   hold. It only ever trades what the bots themselves bought, so coins you had before are never sold.

Orders are market orders on the coin's USD pair; amounts are rounded to Kraken's lot size and
checked against its minimum order size. Fees come back from Kraken with each fill.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import time
import urllib.parse

import httpx

from ..core.models import Fill, Order, OrderStatus
from ..market.kraken import fetch_pairs, fetch_quotes
from .base import Broker

log = logging.getLogger(__name__)

# The ONLY private methods this adapter will ever call. No withdrawals, transfers or staking.
ALLOWED_PRIVATE = frozenset({"Balance", "TradeBalance", "AddOrder", "CancelOrder", "QueryOrders", "OpenOrders",
                             "WithdrawInfo"})  # WithdrawInfo only *reads* fee info; used to prove the key can't withdraw
_STATUS = {"pending": OrderStatus.SUBMITTED, "open": OrderStatus.SUBMITTED, "closed": OrderStatus.FILLED,
           "canceled": OrderStatus.CANCELED, "expired": OrderStatus.CANCELED}


class KrakenError(RuntimeError):
    pass


def sign(path: str, data: dict, secret: str) -> str:
    """Kraken's API-Sign: HMAC-SHA512(base64-decoded secret, path + SHA256(nonce + POST data))."""
    post = urllib.parse.urlencode(data)
    digest = hashlib.sha256((str(data["nonce"]) + post).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), path.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenBroker(Broker):
    name = "kraken"
    label = "Kraken"
    is_live = True
    BASE = "https://api.kraken.com"

    def __init__(self, key: str, secret: str, client: httpx.AsyncClient | None = None) -> None:
        self.key, self.secret = key, secret
        self.client = client or httpx.AsyncClient(base_url=self.BASE, timeout=15.0,
                                                  headers={"User-Agent": "TradeZone/1.0"})
        self.pairs: dict[str, dict] = {}  # "BTC-USD" -> {pair, key, base, lot, ordermin, costmin}
        self.quotes: dict[str, dict] = {}  # "BTC-USD" -> {last, bid, ask, vol_usd, ts}: the "can it be sold?" check
        self.pairs_at = 0.0
        self.symbols: list[str] = []  # what the bots watch (set by the desk) - quotes are kept for these
        # "can it be sold?" limits (the desk copies them from the settings)
        self.max_spread_pct, self.min_volume_usd, self.max_volume_share = 0.015, 250_000.0, 0.01
        self.baseline: dict[str, float] = {}  # asset -> what the owner already held (never traded)
        self._nonce = 0
        self._reported: dict[str, tuple[float, float, float]] = {}  # order id -> (vol, cost, fee)

    # ------------------------------------------------------------------ API
    def _next_nonce(self) -> int:
        self._nonce = max(self._nonce + 1, int(time.time() * 1000))
        return self._nonce

    async def _private(self, method: str, **params) -> dict:
        if method not in ALLOWED_PRIVATE:  # layer 1: this adapter can never move money off Kraken
            raise KrakenError(f"refused: TradeZone never calls Kraken '{method}'")
        path = f"/0/private/{method}"
        data = {"nonce": self._next_nonce(), **params}
        headers = {"API-Key": self.key, "API-Sign": sign(path, data, self.secret),
                   "Content-Type": "application/x-www-form-urlencoded"}
        r = await self.client.post(path, content=urllib.parse.urlencode(data), headers=headers)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise KrakenError("; ".join(body["error"]))
        return body.get("result", {})

    async def load_pairs(self) -> None:
        """Kraken's USD pairs that can be bought AND sold right now (status online), with lot sizes and minimums."""
        self.pairs = await fetch_pairs(self.client)  # replaced, not merged: a pair Kraken suspends disappears
        self.pairs_at = time.time()

    async def prepare(self) -> None:
        await self.refresh_market()

    async def refresh_market(self) -> None:
        """Keep the pair list (every 15 min) and bid / ask / volume (every call) fresh for the sell check."""
        try:
            if not self.pairs or time.time() - self.pairs_at > 900:
                await self.load_pairs()
            if self.symbols:
                self.quotes.update(await fetch_quotes(self.client, self.pairs, self.symbols))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("kraken market refresh failed: %s", exc)

    def min_qty(self, symbol: str, price: float) -> float:
        """The smallest amount Kraken will let you sell (minimum order size and minimum order value)."""
        p = self.pairs.get(symbol)
        if p is None:
            return 0.0
        return max(p["ordermin"], p["costmin"] / price if price > 0 else 0.0)

    def sell_check(self, symbol: str, qty: float, price: float, strict: bool = True) -> str | None:
        """Why buying ``qty`` might leave coins that can't be sold again (None = fine to buy).

        A buy is refused when Kraken doesn't offer the coin for both buying and selling, when the amount
        (or half of it, for the profit-takers) would be under Kraken's minimum sale, or when the market is
        too thin to sell into: a wide bid/ask gap or too little daily trading for the position's size.
        ``strict``: refuse when the market data is missing or stale (real money); otherwise allow."""
        if not self.pairs:
            return "can't confirm it can be sold: Kraken's pair list isn't loaded yet" if strict else None
        if symbol not in self.pairs:
            return f"{symbol} can't be bought and sold on Kraken right now"
        mq = self.min_qty(symbol, price)
        if qty < 2 * mq:
            return (f"too small to sell again: Kraken's minimum sale is {mq:.6g} {symbol.split('-')[0]}, "
                    "and the profit-takers sell in parts")
        q = self.quotes.get(symbol)
        if q is None or time.time() - q["ts"] > 180:
            return "can't confirm it can be sold: no fresh bid / ask from Kraken" if strict else None
        bid, ask = q["bid"], q["ask"]
        if bid <= 0 or ask <= 0:
            return f"nobody is buying {symbol} on Kraken right now"
        spread = (ask - bid) / ask
        if spread > self.max_spread_pct:
            return f"too thin to sell: bid/ask gap {spread:.1%} (max {self.max_spread_pct:.1%})"
        if q["vol_usd"] < self.min_volume_usd:
            return f"too little trading to sell into: ${q['vol_usd']:,.0f} in 24 h (needs ${self.min_volume_usd:,.0f})"
        if qty * price > q["vol_usd"] * self.max_volume_share:
            return f"position would be over {self.max_volume_share:.0%} of a day's trading - too big to exit cleanly"
        return None

    def supports(self, symbol: str) -> bool | None:
        return (symbol in self.pairs) if self.pairs else None  # None = unknown until the pair list loads

    # ------------------------------------------------------------- linking
    async def verify(self) -> dict:
        """Check the key: it must read balances and trade, and it must NOT be able to withdraw."""
        try:
            await self.load_pairs()
            acct = await self.account(raise_errors=True)
        except KrakenError as exc:
            msg = str(exc)
            if "Invalid key" in msg or "Invalid signature" in msg:
                raise ValueError("Kraken didn't accept this key. Check you copied the API key and the private key completely.") from None
            if "Permission denied" in msg:
                raise ValueError('This key can\'t read your balance. Turn on "Query Funds" for the key in Kraken.') from None
            raise ValueError(f"Kraken said: {msg}") from None
        except httpx.HTTPError:
            raise ValueError("Couldn't reach Kraken right now. Try again in a minute.") from None
        # layer 2: a key that could withdraw is refused, even though this code would never use it
        try:
            await self._private("WithdrawInfo", asset="XBT", key="tradezone-permission-check", amount="0.0001")
            can_withdraw = True
        except KrakenError as exc:
            can_withdraw = "Permission denied" not in str(exc)
        except httpx.HTTPError:
            raise ValueError("Couldn't reach Kraken right now. Try again in a minute.") from None
        if can_withdraw:
            raise ValueError('For your safety this key is refused: it has the "Withdraw Funds" permission. '
                             'Make a new key in Kraken with withdrawals turned OFF (see the steps), then paste that one.')
        try:  # can it place orders? (validate=true: Kraken checks the order but never places it)
            await self._private("AddOrder", pair="XBTUSD", type="buy", ordertype="market", volume="0.0001",
                                validate="true")
        except httpx.HTTPError:
            raise ValueError("Couldn't reach Kraken right now. Try again in a minute.") from None
        except KrakenError as exc:
            if "Permission denied" in str(exc):
                raise ValueError('This key can\'t trade. Turn on "Create & Modify Orders" and "Cancel/Close Orders" '
                                 'for the key in Kraken.') from None
        return acct

    # ---------------------------------------------------------- balances
    async def _balances(self) -> dict[str, float]:
        return {k: float(v) for k, v in (await self._private("Balance")).items()}

    async def capture_baseline(self) -> dict[str, float]:
        """Remember what the owner already holds, so the bots never trade those coins."""
        bal = await self._balances()
        self.baseline = {k: v for k, v in bal.items() if v > 0 and k not in ("ZUSD", "USD")}
        return self.baseline

    async def account(self, raise_errors: bool = False) -> dict | None:
        try:
            bal = await self._balances()
            tb = await self._private("TradeBalance", asset="ZUSD")
        except (KrakenError, httpx.HTTPError) as exc:
            if raise_errors:
                raise
            log.warning("kraken account fetch failed: %s", exc)
            return None
        cash = bal.get("ZUSD", bal.get("USD", 0.0))
        return {"cash": cash, "equity": float(tb.get("eb", cash))}

    async def positions(self) -> dict[str, float] | None:
        """Coins the bots bought: what Kraken holds minus what you held before (dust ignored)."""
        try:
            if not self.pairs:
                await self.load_pairs()
            bal = await self._balances()
        except (KrakenError, httpx.HTTPError) as exc:
            log.warning("kraken positions fetch failed: %s", exc)
            return None
        out = {}
        for sym, p in self.pairs.items():
            qty = bal.get(p["base"], 0.0) - self.baseline.get(p["base"], 0.0)
            if qty > max(p["ordermin"], 1e-12):
                out[sym] = qty
        return out

    # ------------------------------------------------------------ orders
    def _volume(self, order: Order) -> str | None:
        p = self.pairs.get(order.symbol)
        if p is None:
            return None
        step = 10 ** -p["lot"]
        vol = math.floor(order.qty / step + 1e-9) * step  # round DOWN: never more than asked
        if vol < p["ordermin"]:
            return None
        return f"{vol:.{p['lot']}f}"

    async def submit(self, order: Order) -> list[Fill]:
        try:
            if not self.pairs:
                await self.load_pairs()
            vol = self._volume(order)
            if vol is None:
                order.status = OrderStatus.REJECTED
                order.reason = (f"kraken: {order.symbol} isn't traded on Kraken" if order.symbol not in self.pairs
                                else f"kraken: below Kraken's minimum order for {order.symbol}")
                return []
            # fciq: Kraken takes its fee in USD, so every coin bought arrives in full and can be sold again
            res = await self._private("AddOrder", pair=self.pairs[order.symbol]["pair"], type=order.side.value,
                                      ordertype="market", volume=vol, oflags="fciq")
        except (KrakenError, httpx.HTTPError) as exc:
            order.status = OrderStatus.REJECTED
            order.reason = f"kraken: {exc}"[:200]
            return []
        order.broker_id = (res.get("txid") or [None])[0]
        order.status = OrderStatus.SUBMITTED
        return await self.poll(order)

    async def poll(self, order: Order) -> list[Fill]:
        if not order.broker_id:
            return []
        try:
            res = await self._private("QueryOrders", txid=order.broker_id, trades="false")
        except (KrakenError, httpx.HTTPError) as exc:
            log.warning("kraken poll %s failed: %s", order.id, exc)
            return []
        info = res.get(order.broker_id) or {}
        order.status = _STATUS.get(info.get("status", ""), order.status)
        vol, cost, fee = float(info.get("vol_exec") or 0), float(info.get("cost") or 0), float(info.get("fee") or 0)
        pv, pc, pf = self._reported.get(order.id, (0.0, 0.0, 0.0))
        if vol > pv and vol > 0:
            order.status = OrderStatus.FILLED if order.status is OrderStatus.FILLED else OrderStatus.PARTIALLY_FILLED
        order.filled_qty = vol
        order.avg_fill_price = cost / vol if vol else 0.0
        if vol <= pv + 1e-12:
            return []
        self._reported[order.id] = (vol, cost, fee)
        dv = vol - pv
        return [Fill(order.id, order.symbol, order.side, dv, (cost - pc) / dv, fee - pf)]

    async def cancel(self, order: Order) -> None:
        if order.broker_id:
            try:
                await self._private("CancelOrder", txid=order.broker_id)
            except (KrakenError, httpx.HTTPError) as exc:
                log.warning("kraken cancel %s failed: %s", order.id, exc)

    async def close(self) -> None:
        await self.client.aclose()
