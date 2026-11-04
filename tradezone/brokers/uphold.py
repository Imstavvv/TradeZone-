"""Uphold adapter (real money) via the Uphold REST API (``https://api.uphold.com/v0``).

Uphold has no order book API: a trade is a *transaction* that moves value from one of your
cards (a balance in one currency) to another. So:

* **buy** ``DOGE-USD`` = send from your USD card to your DOGE card, denominated in DOGE;
* **sell** ``DOGE-USD`` = send from your DOGE card to your USD card, denominated in DOGE.

Transactions are created and committed in one call (``?commit=true``) and normally complete
instantly. Uphold's cost is built into the rate (the *spread*), not charged as a separate fee.
The adapter reports a fill at the live market price and books the spread as ``commission``, so
the team's P&L includes every cent Uphold keeps, and the slippage guard measures real slippage
(price moving against us) rather than flagging Uphold's normal spread on every fill.

Authentication is a bearer token: a Personal Access Token (``python -m tradezone uphold-token``
makes one) or an OAuth access token.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..core.models import Fill, Order, OrderStatus, Side, Tick
from .base import Broker

log = logging.getLogger(__name__)

#: Uphold transaction status -> our order status
_STATUS = {
    "completed": OrderStatus.FILLED,
    "pending": OrderStatus.SUBMITTED,
    "processing": OrderStatus.SUBMITTED,
    "waiting": OrderStatus.SUBMITTED,
    "cancelled": OrderStatus.CANCELED,
    "canceled": OrderStatus.CANCELED,
    "failed": OrderStatus.REJECTED,
    "deleted": OrderStatus.REJECTED,
}
#: fiat and stable balances never count as a "position" the bots hold
_CASH_LIKE = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY", "MXN", "USDC", "USDT", "UPUSD"}
DUST_USD = 0.01  # ignore balances worth less than a cent


def _amount(qty: float) -> str:
    """Uphold amounts are decimal strings; never send exponent notation (1e-05)."""
    return f"{qty:.8f}".rstrip("0").rstrip(".") or "0"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def split_symbol(symbol: str) -> tuple[str, str]:
    s = symbol.upper().replace("/", "-")
    base, _, quote = s.partition("-")
    return base, quote or "USD"


class UpholdBroker(Broker):
    name = "uphold"
    label = "Uphold"
    is_live = True
    BASE = "https://api.uphold.com"
    CARDS_TTL_S = 300.0  # card ids barely change; balances are always fetched fresh

    def __init__(self, token: str, client: httpx.AsyncClient | None = None, quote: str = "USD",
                 symbols: list[str] | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            base_url=self.BASE, timeout=15.0,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "TradeZone/1.0"},
        )
        self.quote = quote.upper()
        # only these coins count as "the bots' positions" (your other Uphold coins are left alone)
        self.bases = {split_symbol(s)[0] for s in symbols} if symbols else None
        self.prices: dict[str, float] = {}
        self.assets: set[str] | None = None  # tradeable currency codes, once loaded
        self._cards: dict[str, str] = {}  # currency -> card id
        self._cards_at = 0.0
        self._lock = asyncio.Lock()
        self.last_error = ""

    # ------------------------------------------------------------ helpers
    def on_prices(self, ticks: list[Tick]) -> None:
        for t in ticks:
            self.prices[t.symbol] = t.price

    def supports(self, symbol: str) -> bool | None:
        base, quote = split_symbol(symbol)
        if "-" not in symbol and "/" not in symbol:
            return False  # Uphold has no stocks
        if quote != self.quote:
            return False
        if self.assets is None:
            return None  # not known yet: the order itself will tell
        return base in self.assets

    async def load_assets(self) -> set[str] | None:
        try:
            r = await self.client.get("/v0/assets")
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:
            self.last_error = f"assets: {type(exc).__name__}: {exc}"[:200]
            log.warning("uphold assets failed: %s", exc)
            return None
        self.assets = {a["code"].upper() for a in rows if isinstance(a, dict) and a.get("code")
                       and a.get("status", "open") == "open"}
        return self.assets

    async def prepare(self) -> None:
        await self.load_assets()

    async def _get_cards(self) -> list[dict] | None:
        try:
            r = await self.client.get("/v0/me/cards")
            r.raise_for_status()
            cards = r.json()
        except Exception as exc:
            self.last_error = f"cards: {type(exc).__name__}: {exc}"[:200]
            log.warning("uphold cards fetch failed: %s", exc)
            return None
        if not isinstance(cards, list):
            return None
        best: dict[str, tuple[float, str]] = {}
        for c in cards:
            cur, cid = str(c.get("currency", "")).upper(), c.get("id")
            if cur and cid and (cur not in best or _num(c.get("available")) > best[cur][0]):
                best[cur] = (_num(c.get("available")), cid)  # several cards per coin: use the fullest
        self._cards = {cur: cid for cur, (_, cid) in best.items()}
        self._cards_at = time.monotonic()
        return cards

    async def _card(self, currency: str, create: bool = False) -> str | None:
        async with self._lock:
            if currency not in self._cards or time.monotonic() - self._cards_at > self.CARDS_TTL_S:
                await self._get_cards()
            if currency in self._cards or not create:
                return self._cards.get(currency)
            try:  # first trade in this coin: open a card for it
                r = await self.client.post("/v0/me/cards", json={"label": f"TradeZone {currency}", "currency": currency})
                r.raise_for_status()
                self._cards[currency] = r.json()["id"]
            except Exception as exc:
                self.last_error = f"create {currency} card: {type(exc).__name__}: {exc}"[:200]
                log.warning("uphold create card %s failed: %s", currency, exc)
                return None
            return self._cards[currency]

    def _fills_from(self, order: Order, tx: dict) -> list[Fill]:
        status = str(tx.get("status", "")).lower()
        order.status = _STATUS.get(status, OrderStatus.SUBMITTED)
        if order.status is OrderStatus.REJECTED:
            order.reason = f"uphold transaction {status}"
        if order.status is not OrderStatus.FILLED or order.filled_qty > 0:
            return []
        origin, dest = tx.get("origin") or {}, tx.get("destination") or {}
        if order.side is Side.BUY:
            coins, usd = _num(dest.get("amount")), _num(origin.get("amount"))
            qty = min(order.qty, coins) if coins > 0 else order.qty
        else:
            qty, usd = order.qty, _num(dest.get("amount"))
        if qty <= 0 or usd <= 0:
            order.status = OrderStatus.REJECTED
            order.reason = "uphold returned a transaction without amounts"
            return []
        eff = usd / qty  # what each coin really cost / paid, spread included
        price, commission = eff, 0.0
        ref = self.prices.get(order.symbol)
        if ref and ref > 0:
            cost = (usd - qty * ref) if order.side is Side.BUY else (qty * ref - usd)
            if cost > 0:  # the spread: book it as a cost at the market price
                price, commission = ref, cost
        order.filled_qty, order.avg_fill_price = qty, eff
        return [Fill(order.id, order.symbol, order.side, qty, price, commission)]

    # -------------------------------------------------------------- orders
    async def submit(self, order: Order) -> list[Fill]:
        base, quote = split_symbol(order.symbol)
        if quote != self.quote or order.qty <= 0:
            order.status = OrderStatus.REJECTED
            order.reason = f"uphold: can't trade {order.symbol}" if quote != self.quote else "invalid quantity"
            return []
        buy = order.side is Side.BUY
        origin = await self._card(quote if buy else base)
        dest = await self._card(base if buy else quote, create=True)
        if not origin or not dest:
            order.status = OrderStatus.REJECTED
            order.reason = (f"uphold: no {quote if buy else base} balance to trade from" if not origin
                            else f"uphold: couldn't open a {base if buy else quote} card") + \
                (f" ({self.last_error})" if self.last_error else "")
            return []
        body = {"denomination": {"amount": _amount(order.qty), "currency": base}, "destination": dest,
                "message": f"TradeZone {order.id}"}
        try:
            r = await self.client.post(f"/v0/me/cards/{origin}/transactions", params={"commit": "true"}, json=body)
            if r.status_code >= 400:
                order.status = OrderStatus.REJECTED
                order.reason = f"uphold {r.status_code}: {r.text[:200]}"
                return []
            tx = r.json()
        except httpx.HTTPError as exc:
            order.status = OrderStatus.REJECTED
            order.reason = f"uphold transport error: {exc}"
            return []
        order.broker_id = tx.get("id")
        return self._fills_from(order, tx)

    async def poll(self, order: Order) -> list[Fill]:
        if not order.broker_id or order.status.is_terminal:
            return []
        try:
            r = await self.client.get(f"/v0/me/transactions/{order.broker_id}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("uphold poll %s failed: %s", order.id, exc)
            return []
        return self._fills_from(order, r.json())

    # ------------------------------------------------------------- account
    async def account(self) -> dict | None:
        cards = await self._get_cards()
        if cards is None:
            return None
        cash = sum(_num(c.get("available")) for c in cards if str(c.get("currency", "")).upper() == self.quote)
        equity = 0.0
        for c in cards:
            norm = next((n for n in c.get("normalized") or [] if str(n.get("currency", "")).upper() == self.quote), None)
            equity += _num(norm.get("balance")) if norm else (
                _num(c.get("balance")) if str(c.get("currency", "")).upper() == self.quote else 0.0)
        return {"cash": cash, "equity": max(equity, cash)}

    async def positions(self) -> dict[str, float] | None:
        cards = await self._get_cards()
        if cards is None:
            return None
        out: dict[str, float] = {}
        for c in cards:
            cur = str(c.get("currency", "")).upper()
            if cur in _CASH_LIKE or (self.bases is not None and cur not in self.bases):
                continue
            bal = _num(c.get("balance"))
            norm = next((n for n in c.get("normalized") or [] if str(n.get("currency", "")).upper() == self.quote), None)
            if bal <= 0 or (norm is not None and _num(norm.get("balance")) < DUST_USD):
                continue
            sym = f"{cur}-{self.quote}"
            out[sym] = out.get(sym, 0.0) + bal
        return out

    async def close(self) -> None:
        await self.client.aclose()


async def create_personal_access_token(username: str, password: str, otp_code: str | None,
                                       description: str = "TradeZone",
                                       client: httpx.AsyncClient | None = None) -> str:
    """Make an Uphold Personal Access Token (used by ``python -m tradezone uphold-token``).

    Your password is sent only to Uphold, over HTTPS, and is never stored.
    """
    c = client or httpx.AsyncClient(base_url=UpholdBroker.BASE, timeout=15.0,
                                    headers={"User-Agent": "TradeZone/1.0"})
    auth = httpx.BasicAuth(username, password)
    try:
        headers = {}
        if otp_code:
            r = await c.get("/v0/me/authentication_methods", auth=auth)
            r.raise_for_status()
            methods = [m for m in r.json() if m.get("verified", True)]
            method = next((m for m in methods if m.get("default")), methods[0] if methods else None)
            if method:
                headers = {"OTP-Method-Id": str(method["id"]), "OTP-Token": otp_code}
        r = await c.post("/v0/me/tokens", auth=auth, headers=headers, json={"description": description})
        if r.status_code >= 400:
            raise ValueError(f"Uphold refused ({r.status_code}): {r.text[:300]}")
        return r.json()["accessToken"]
    finally:
        if client is None:
            await c.aclose()
