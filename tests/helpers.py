"""Shared test helpers (one copy, imported by every test module)."""

import asyncio
import base64
import hashlib
import json
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from tradezone.brokers.paper import PaperBroker
from tradezone.core.models import Snapshot, Tick


def run(coro):
    return asyncio.run(coro)


def prime(desk, symbol="AAPL", price=100.0, atr=0.5):
    """Give the desk a live price and indicator snapshot for ``symbol`` without running the feed."""
    desk.prices[symbol] = price
    desk.broker.on_prices([Tick(symbol, price)])
    desk.portfolio.mark(symbol, price)
    ind = {"atr": atr, "volatility": 0.003, "rsi": 50, "ema_fast": price, "ema_slow": price,
           "sma50": price, "donchian_high": price, "donchian_low": price}
    desk.bots["risk"].snapshots[symbol] = Snapshot(symbol, price, ind)


def capture(desk, topic):
    out = []
    desk.bus.subscribe(topic, lambda e: out.append(e.payload), owner="test")
    return out


class FakeLiveBroker(PaperBroker):
    """Stands in for the Alpaca live account."""

    name = "alpaca"
    is_live = True

    def __init__(self, cash=2_500.0, positions=None):
        super().__init__(0, 0)
        self.cash, self._positions, self.submitted = cash, positions or {}, []

    async def submit(self, order):
        self.submitted.append(order)
        return await super().submit(order)

    async def account(self):
        return {"cash": self.cash, "equity": self.cash}

    async def positions(self):
        return dict(self._positions)


ORIGIN, RP_ID = "https://testserver", "testserver"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    """A minimal platform authenticator (ES256), standing in for the iPhone's Face ID secure enclave."""

    def __init__(self, user_verified=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = b"\x01" * 16
        self.count = 0
        self.uv = user_verified

    def _flags(self, attested=False):
        f = 0x01 | (0x04 if self.uv else 0)  # user present (+ user verified = Face ID passed)
        return f | (0x40 if attested else 0)

    def create(self, opts):
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth_data = (hashlib.sha256(opts["rp"]["id"].encode()).digest() + bytes([self._flags(True)])
                     + struct.pack(">I", self.count) + b"\x00" * 16 + struct.pack(">H", len(self.cred_id))
                     + self.cred_id + cose)
        cdj = json.dumps({"type": "webauthn.create", "challenge": opts["challenge"], "origin": ORIGIN}).encode()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att)},
                "clientExtensionResults": {}}

    def get(self, opts, origin=ORIGIN):
        self.count += 1
        auth_data = (hashlib.sha256(opts["rpId"].encode()).digest() + bytes([self._flags()])
                     + struct.pack(">I", self.count))
        cdj = json.dumps({"type": "webauthn.get", "challenge": opts["challenge"], "origin": origin}).encode()
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig)}, "clientExtensionResults": {}}
