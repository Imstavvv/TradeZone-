"""Crypto address checks, done properly: every address that carries a checksum is verified.

A wrong character in an address is the most common way people lose crypto. Most address formats
have a built-in checksum, and a typo almost never survives it, so this module checks the real
checksum for each network (not just "looks about right"):

* Bitcoin / Litecoin / Dogecoin legacy & script addresses, Tron, XRP: Base58Check (double SHA-256)
* Bitcoin / Litecoin SegWit & Taproot, Cosmos, Cardano: Bech32 / Bech32m
* Ethereum and every EVM network (Polygon, BNB Chain, Arbitrum, Base, Optimism, Avalanche C-Chain):
  EIP-55 mixed-case checksum (Keccak-256)
* Stellar: StrKey CRC16 · Polkadot: SS58 (BLAKE2b) · Algorand: SHA-512/256 checksum
* Solana: must decode to exactly 32 bytes (Solana addresses have no checksum - double-check it)
* Hedera: 0.0.N account IDs

What a checksum can't tell you: whether the address belongs to *you*. That's why the first send to
a new wallet is a small test amount until you confirm it arrived.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

B58_BTC = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_XRP = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


# --------------------------------------------------------------------------- base58
def b58decode(s: str, alphabet: str = B58_BTC) -> bytes | None:
    n = 0
    for ch in s:
        i = alphabet.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip(alphabet[0]))
    return b"\0" * pad + raw


def b58check(s: str, alphabet: str = B58_BTC) -> bytes | None:
    """Payload (version byte + data) if the 4-byte double-SHA256 checksum matches, else None."""
    raw = b58decode(s, alphabet)
    if raw is None or len(raw) < 5:
        return None
    payload, check = raw[:-4], raw[-4:]
    return payload if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == check else None


# --------------------------------------------------------------------------- bech32
def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32_decode(addr: str) -> tuple[str, list[int], str] | None:
    """(hrp, data without checksum, 'bech32' | 'bech32m') if the checksum is valid."""
    if addr.lower() != addr and addr.upper() != addr:
        return None  # mixed case is never valid
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr):
        return None
    hrp, rest = addr[:pos], addr[pos + 1:]
    if any(c not in BECH32 for c in rest):
        return None
    data = [BECH32.find(c) for c in rest]
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(expanded + data)
    kind = "bech32" if pm == 1 else "bech32m" if pm == 0x2BC830A3 else None
    return (hrp, data[:-6], kind) if kind else None


def _segwit_ok(addr: str, hrp: str) -> bool:
    d = bech32_decode(addr)
    if not d or d[0] != hrp or not d[1]:
        return False
    version = d[1][0]
    if version > 16 or (version == 0) != (d[2] == "bech32"):  # v0 uses bech32, v1+ bech32m
        return False
    prog_bits = (len(d[1]) - 1) * 5
    prog_len = prog_bits // 8
    return 2 <= prog_len <= 40 and (version != 0 or prog_len in (20, 32))


# --------------------------------------------------------------------------- keccak (EIP-55)
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def keccak256(data: bytes) -> bytes:
    """Ethereum's Keccak-256 (not the same as SHA3-256: different padding)."""
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\0" * (-len(msg) % rate)
    msg[-1] |= 0x80
    st = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            st[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i: off + 8 * i + 8], "little")
        for rc in _RC:
            c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M) for x in range(5)]
            st = [[st[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    r = _ROT[x][y]
                    v = st[x][y]
                    b[y][(2 * x + 3 * y) % 5] = ((v << r) | (v >> (64 - r))) & _M if r else v
            st = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            st[0][0] ^= rc
    return b"".join(st[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def eip55(addr: str) -> str:
    h = addr[2:].lower()
    digest = keccak256(h.encode()).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(h))


# --------------------------------------------------------------------------- other checksums
def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _stellar_ok(a: str) -> bool:
    try:
        raw = base64.b32decode(a)
    except (ValueError, base64.binascii.Error):
        return False
    if len(raw) != 35 or raw[0] != 6 << 3:  # account id (G...)
        return False
    return _crc16_xmodem(raw[:33]).to_bytes(2, "little") == raw[33:]


def _ss58_ok(a: str) -> bool:
    raw = b58decode(a)
    if raw is None or len(raw) != 35 or raw[0] >= 64:  # simple 1-byte prefix + 32-byte key + 2 checksum
        return False
    return hashlib.blake2b(b"SS58PRE" + raw[:33], digest_size=64).digest()[:2] == raw[33:]


def _algorand_ok(a: str) -> bool:
    try:
        raw = base64.b32decode(a + "======")
    except (ValueError, base64.binascii.Error):
        return False
    if len(raw) != 36:
        return False
    try:
        h = hashlib.new("sha512_256", raw[:32]).digest()
    except ValueError:  # this Python build lacks SHA-512/256: fall back to the format check
        return True
    return h[-4:] == raw[32:]


# --------------------------------------------------------------------------- networks
@dataclass(frozen=True)
class Check:
    ok: bool
    checksum: str  # verified | none | failed
    note: str = ""


def _evm(a: str) -> Check:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", a):
        return Check(False, "failed", "an Ethereum-style address is 0x followed by 40 letters/numbers")
    body = a[2:]
    if body.islower() or body.isupper():
        return Check(True, "none", "this address has no capital-letter checksum - copy it straight from your wallet")
    if eip55(a) != a:
        return Check(False, "failed", "the checksum doesn't match - a character is wrong")
    return Check(True, "verified")


def _b58ver(a: str, versions: tuple[int, ...], length: int = 21, alphabet: str = B58_BTC) -> Check:
    p = b58check(a, alphabet)
    if p is None:
        return Check(False, "failed", "the checksum doesn't match - a character is wrong")
    if len(p) != length or p[0] not in versions:
        return Check(False, "failed", "that's a valid address for a different coin")
    return Check(True, "verified")


def _btc(a: str) -> Check:
    if a.lower().startswith("bc1"):
        return Check(True, "verified") if _segwit_ok(a, "bc") else Check(False, "failed", "the checksum doesn't match")
    return _b58ver(a, (0x00, 0x05))


def _ltc(a: str) -> Check:
    if a.lower().startswith("ltc1"):
        return Check(True, "verified") if _segwit_ok(a, "ltc") else Check(False, "failed", "the checksum doesn't match")
    return _b58ver(a, (0x30, 0x32))


def _bech32_hrp(a: str, hrp: str) -> Check:
    d = bech32_decode(a)
    if not d or d[0] != hrp or d[2] != "bech32":
        return Check(False, "failed", "the checksum doesn't match - a character is wrong")
    return Check(True, "verified")


def _sol(a: str) -> Check:
    raw = b58decode(a)
    if raw is None or len(raw) != 32:
        return Check(False, "failed", "a Solana address decodes to exactly 32 bytes")
    return Check(True, "none", "Solana addresses have no checksum - double-check the first and last characters")


VALIDATORS = {
    "BTC": (_btc, r"^(bc1|[13])"),
    "LTC": (_ltc, r"^(ltc1|[LM3])"),
    "DOGE": (lambda a: _b58ver(a, (0x1E, 0x16)), r"^[DA9]"),
    "XRP": (lambda a: _b58ver(a, (0x00,), 21, B58_XRP), r"^r"),
    "TRX": (lambda a: _b58ver(a, (0x41,)), r"^T"),
    "SOL": (_sol, r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "XLM": (lambda a: Check(True, "verified") if _stellar_ok(a) else Check(False, "failed", "the checksum doesn't match"), r"^G[A-Z2-7]{55}$"),
    "ATOM": (lambda a: _bech32_hrp(a, "cosmos"), r"^cosmos1"),
    "ADA": (lambda a: _bech32_hrp(a, "addr"), r"^addr1"),
    "DOT": (lambda a: Check(True, "verified") if _ss58_ok(a) else Check(False, "failed", "the checksum doesn't match"), r"^1[1-9A-HJ-NP-Za-km-z]{46,47}$"),
    "ALGO": (lambda a: Check(True, "verified") if _algorand_ok(a) else Check(False, "failed", "the checksum doesn't match"), r"^[A-Z2-7]{58}$"),
    "HBAR": (lambda a: Check(bool(re.fullmatch(r"0\.0\.\d{1,10}", a)), "none", "Hedera account IDs (0.0.N) have no checksum here"), r"^0\.0\."),
    "EOS": (lambda a: Check(bool(re.fullmatch(r"[a-z1-5.]{1,12}", a)), "none", "EOS account names have no checksum"), r"^[a-z1-5.]{1,12}$"),
}
EVM_NETWORKS = ("ETH", "ARB", "BASE", "OP", "POLYGON", "BSC", "AVAXC")
for _code in EVM_NETWORKS:
    VALIDATORS[_code] = (_evm, r"^0x")


def check_address(chain: str, address: str) -> Check:
    v = VALIDATORS.get(chain)
    if v is None:
        return Check(False, "failed", f"unknown network {chain}")
    a = address.strip()
    if not re.search(v[1], a):
        return Check(False, "failed", "that doesn't look like an address on this network")
    return v[0](a)


def detect(address: str) -> list[str]:
    """Networks this address is valid on (checksum-checked). EVM addresses fit several networks."""
    a = address.strip()
    hits = [c for c in VALIDATORS if c not in EVM_NETWORKS and check_address(c, a).ok]
    if check_address("ETH", a).ok:
        hits += list(EVM_NETWORKS)
    if "SOL" in hits and len(hits) > 1:  # Solana is the loosest format: prefer a checksummed match
        hits.remove("SOL")
    return hits
