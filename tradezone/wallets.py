"""Wallet linking, deposits and withdrawals, with destination tag / memo support.

* **Linking:** every address is checked against its network's real checksum (see addresses.py),
  so a typo is caught before any money moves. Chains that use a destination tag or memo
  (XRP, XLM, ATOM, HBAR, EOS) validate it, and if you leave it empty you must confirm that
  the destination doesn't need one. That prevents the classic "missing tag = lost funds" mistake.
* **Deposit tags:** every linked wallet gets a unique numeric deposit tag. Deposits to the desk's
  tag-based deposit addresses that carry the tag are credited to that wallet. Deposits on chains
  without tags are matched by sender address. Anything unmatched is held for manual review,
  never auto-credited.
* **Withdrawals:** only to linked wallets, only after a security cooldown, within a daily limit
  and available cash, and always with the wallet's tag attached. The first send to a new wallet is
  a small *test* (``TZ_TEST_SEND_USD``); full amounts unlock once you confirm it arrived.

In paper mode deposits and withdrawals move simulated cash. In live mode the desk has no custody
of funds (they sit at your broker), so withdrawals are recorded as ``manual_required`` and are not
debited until you complete them at the broker.
"""

from __future__ import annotations

import json
import math
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .addresses import check_address, detect
from .core.models import Alert

if TYPE_CHECKING:
    from .desk import TradingDesk

@dataclass(frozen=True)
class TagRule:
    name: str  # what the chain calls it
    numeric: bool  # uint32 destination tag vs free-text memo
    max_len: int


@dataclass(frozen=True)
class Chain:
    code: str
    name: str
    assets: tuple[str, ...]
    tag: TagRule | None = None
    fee: str = ""  # typical network fee, so you can pick a cheap way to move money
    fee_usd: float = 1.0  # rough upper estimate of what one transfer out costs (exchange withdrawal fee), in USD


MEMO = TagRule("memo", False, 256)
CHAINS: dict[str, Chain] = {
    c.code: c
    for c in [
        Chain("BTC", "Bitcoin", ("BTC",), fee="about $1-5", fee_usd=5.0),
        Chain("ETH", "Ethereum", ("ETH", "USDC", "USDT"), fee="about $0.50-5", fee_usd=5.0),
        Chain("BASE", "Base (Ethereum L2)", ("ETH", "USDC"), fee="a few cents", fee_usd=0.5),
        Chain("ARB", "Arbitrum (Ethereum L2)", ("ETH", "USDC", "USDT"), fee="a few cents", fee_usd=0.5),
        Chain("OP", "Optimism (Ethereum L2)", ("ETH", "USDC", "USDT"), fee="a few cents", fee_usd=0.5),
        Chain("POLYGON", "Polygon", ("POL", "USDC", "USDT"), fee="a few cents", fee_usd=0.3),
        Chain("BSC", "BNB Smart Chain", ("BNB", "USDC", "USDT"), fee="a few cents", fee_usd=0.3),
        Chain("AVAXC", "Avalanche C-Chain", ("AVAX", "USDC", "USDT"), fee="a few cents", fee_usd=0.5),
        Chain("SOL", "Solana", ("SOL", "USDC", "USDT"), fee="under a cent", fee_usd=0.3),
        Chain("TRX", "Tron", ("TRX", "USDT"), fee="about $1 for USDT", fee_usd=1.5),
        Chain("LTC", "Litecoin", ("LTC",), fee="a few cents", fee_usd=0.2),
        Chain("DOGE", "Dogecoin", ("DOGE",), fee="a few cents", fee_usd=0.5),
        Chain("ADA", "Cardano", ("ADA",), fee="about $0.20", fee_usd=0.5),
        Chain("DOT", "Polkadot", ("DOT",), fee="a few cents", fee_usd=0.3),
        Chain("ALGO", "Algorand", ("ALGO", "USDC"), fee="under a cent", fee_usd=0.1),
        Chain("XRP", "XRP Ledger", ("XRP",), TagRule("destination tag", True, 10), fee="under a cent", fee_usd=0.2),
        Chain("XLM", "Stellar", ("XLM", "USDC"), TagRule("memo", False, 28), fee="under a cent", fee_usd=0.1),
        Chain("ATOM", "Cosmos", ("ATOM",), MEMO, fee="a few cents", fee_usd=0.2),
        Chain("HBAR", "Hedera", ("HBAR",), TagRule("memo", False, 100), fee="under a cent", fee_usd=0.1),
        Chain("EOS", "EOS", ("EOS",), MEMO, fee="free", fee_usd=0.1),
    ]
}

STABLES = {"USD", "USDC", "USDT"}


class WalletError(ValueError):
    pass


def validate_tag(chain: Chain, tag: str | None) -> str | None:
    if tag is None or tag == "":
        return None
    if chain.tag is None:
        raise WalletError(f"{chain.name} does not use destination tags or memos")
    tag = str(tag).strip()
    if chain.tag.numeric:
        if not tag.isdigit() or int(tag) > 4_294_967_295:
            raise WalletError(f"{chain.name} {chain.tag.name} must be an integer 0-4294967295")
        return str(int(tag))
    if len(tag.encode()) > chain.tag.max_len:
        raise WalletError(f"{chain.name} {chain.tag.name} must be at most {chain.tag.max_len} bytes")
    return tag


@dataclass
class Wallet:
    id: str
    chain: str
    address: str
    tag: str | None
    label: str
    deposit_tag: str
    linked_at: float
    verified: bool = False  # the owner confirmed a test send arrived: full amounts allowed
    checksum: str = ""  # verified | none (the network has no checksum)

    @property
    def fee_usd(self) -> float:
        return CHAINS[self.chain].fee_usd

    def to_dict(self, cooldown_h: float = 0.0) -> dict[str, Any]:
        d = asdict(self)
        chain = CHAINS[self.chain]
        d["chain_name"] = chain.name
        d["fee"] = chain.fee
        d["fee_usd"] = chain.fee_usd
        d["tag_name"] = chain.tag.name if chain.tag else None
        d["assets"] = list(chain.assets)
        d["withdrawals_enabled_at"] = self.linked_at + cooldown_h * 3600
        return d


@dataclass
class Transfer:
    id: str
    kind: str  # deposit | withdrawal
    chain: str
    asset: str
    amount: float
    usd_value: float
    status: str  # completed | held | manual_required
    wallet_id: str | None = None
    address: str | None = None
    tag: str | None = None
    tx_hash: str | None = None
    note: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transfers (id TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT NOT NULL);
"""


class WalletService:
    def __init__(self, desk: TradingDesk) -> None:
        self.desk = desk
        self.db = desk.journal
        with self.db._lock:
            self.db.conn.executescript(SCHEMA)
            rows = self.db.conn.execute("SELECT data FROM wallets").fetchall()
            trows = self.db.conn.execute("SELECT data FROM transfers ORDER BY ts").fetchall()
        self.wallets: dict[str, Wallet] = {}
        for (data,) in rows:
            w = Wallet(**json.loads(data))
            self.wallets[w.id] = w
        self.transfers: list[Transfer] = [Transfer(**json.loads(d)) for (d,) in trows]
        self.deposit_addresses: dict[str, str] = {}
        for entry in desk.settings.deposit_addresses:
            chain, _, addr = entry.partition(":")
            chain = chain.strip().upper()
            if chain not in CHAINS or not check_address(chain, addr.strip()).ok:
                raise ValueError(f"invalid TZ_DEPOSIT_ADDRESSES entry {entry!r}")
            self.deposit_addresses[chain] = addr.strip()

    # -- persistence ------------------------------------------------------
    def _save_wallet(self, w: Wallet) -> None:
        self.db.run("INSERT OR REPLACE INTO wallets(id, data) VALUES (?,?)", (w.id, json.dumps(asdict(w))))

    def _save_transfer(self, t: Transfer) -> None:
        self.transfers.append(t)
        self.db.run(
            "INSERT OR REPLACE INTO transfers(id, ts, data) VALUES (?,?,?)",
            (t.id, t.ts, json.dumps(t.to_dict())),
        )

    def _event(self, topic: str, payload: dict) -> None:
        self.desk.bus.publish(topic, payload, source="treasury")

    # -- wallets ----------------------------------------------------------
    def link(self, chain: str, address: str, tag: str | None = None, label: str = "",
             no_tag_confirmed: bool = False) -> Wallet:
        code = (chain or "").strip().upper()
        if code not in CHAINS:
            raise WalletError(f"unsupported chain {chain!r}; supported: {', '.join(CHAINS)}")
        c = CHAINS[code]
        address = (address or "").strip()
        chk = check_address(code, address)
        if not chk.ok:
            raise WalletError(f"That isn't a valid {c.name} address: {chk.note}.")
        tag = validate_tag(c, tag)
        if c.tag and tag is None and not no_tag_confirmed:
            raise WalletError(
                f"{c.name} transfers to exchanges usually need a {c.tag.name}. Enter it, or confirm "
                f"this address doesn't need one (no_tag_confirmed) - a missing {c.tag.name} can lose funds."
            )
        for w in self.wallets.values():
            if w.chain == code and w.address == address and w.tag == tag:
                raise WalletError("this wallet (address + tag) is already linked")
        used = {w.deposit_tag for w in self.wallets.values()}
        dtag = str(secrets.randbelow(4_000_000_000) + 100_000)
        while dtag in used:
            dtag = str(secrets.randbelow(4_000_000_000) + 100_000)
        w = Wallet(f"w_{secrets.token_hex(6)}", code, address, tag, label.strip()[:60] or f"{c.name} wallet",
                   dtag, time.time(), checksum=chk.checksum)
        self.wallets[w.id] = w
        self._save_wallet(w)
        self._event("wallet.linked", {"id": w.id, "chain": code, "address": address, "tag": tag})
        return w

    @staticmethod
    def check(address: str) -> dict:
        """What network(s) an address belongs to, checksum-verified - for the 'add a wallet' screen."""
        a = (address or "").strip()
        for prefix in ("bitcoin:", "litecoin:", "dogecoin:", "ethereum:", "solana:"):  # payment links
            if a.lower().startswith(prefix):
                a = a[len(prefix):].split("?")[0].split("@")[0]
        hits = detect(a)
        return {"address": a, "networks": [
            {"chain": c, "name": CHAINS[c].name, "fee": CHAINS[c].fee, "assets": list(CHAINS[c].assets),
             "tag_name": CHAINS[c].tag.name if CHAINS[c].tag else None, **vars(check_address(c, a))}
            for c in hits if c in CHAINS]}

    def confirm_received(self, wallet_id: str) -> Wallet:
        """The owner saw the test send arrive: this wallet is proven, full amounts are allowed."""
        w = self.wallets.get(wallet_id)
        if w is None:
            raise WalletError("unknown wallet")
        if not any(t.wallet_id == w.id and t.kind == "withdrawal" for t in self.transfers):
            raise WalletError("send a small test amount to this wallet first, then confirm it arrived")
        w.verified = True
        self._save_wallet(w)
        self._event("wallet.verified", {"id": w.id})
        return w

    def unlink(self, wallet_id: str) -> None:
        if wallet_id not in self.wallets:
            raise WalletError("unknown wallet")
        del self.wallets[wallet_id]
        self.db.run("DELETE FROM wallets WHERE id = ?", (wallet_id,))
        self._event("wallet.removed", {"id": wallet_id})

    # -- pricing ----------------------------------------------------------
    def usd_price(self, asset: str) -> float:
        asset = asset.upper()
        if asset in STABLES:
            return 1.0
        px = self.desk.prices.get(f"{asset}-USD")
        if not px:
            raise WalletError(f"TradeZone doesn't have a live price for {asset} right now, so it can't work out the "
                              f"dollar value. Try again in a moment (server setting: add {asset}-USD to TZ_SYMBOLS).")
        return px

    # -- deposits ---------------------------------------------------------
    def deposit_info(self) -> list[dict]:
        out = []
        for w in self.wallets.values():
            c = CHAINS[w.chain]
            out.append({
                "wallet_id": w.id, "chain": w.chain, "label": w.label,
                "deposit_address": self.deposit_addresses.get(w.chain),
                "deposit_tag": w.deposit_tag if c.tag else None,
                "tag_name": c.tag.name if c.tag else None,
                "match_by": "deposit tag" if c.tag else "sender address",
            })
        return out

    def record_deposit(self, chain: str, asset: str, amount: float, tx_hash: str,
                       tag: str | None = None, from_address: str | None = None) -> Transfer:
        code = chain.strip().upper()
        if code not in CHAINS:
            raise WalletError(f"unsupported chain {chain!r}")
        c = CHAINS[code]
        asset = asset.strip().upper()
        if asset not in c.assets:
            raise WalletError(f"{asset} is not supported on {c.name}")
        if not (isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0):
            raise WalletError("amount must be a positive number")
        tx_hash = (tx_hash or "").strip()
        if not tx_hash:
            raise WalletError("tx_hash is required (used to prevent double-crediting)")
        if any(t.tx_hash == tx_hash and t.kind == "deposit" for t in self.transfers):
            raise WalletError("deposit with this tx_hash was already recorded")
        usd = round(amount * self.usd_price(asset), 2)
        wallet = None
        if c.tag:
            tag = validate_tag(c, tag)
            wallet = next((w for w in self.wallets.values() if w.chain == code and w.deposit_tag == tag), None)
        elif from_address:
            wallet = next((w for w in self.wallets.values()
                           if w.chain == code and w.address.lower() == from_address.strip().lower()), None)
        t = Transfer(f"dep_{secrets.token_hex(6)}", "deposit", code, asset, amount, usd,
                     "completed" if wallet else "held", wallet.id if wallet else None,
                     from_address, tag, tx_hash)
        if wallet and self.desk.is_live:
            t.note = "live mode: recorded only - your broker balance is the source of truth for trading capital"
        elif wallet:
            t.note = f"credited via {'deposit tag' if c.tag else 'sender address'} to {wallet.label}"
            self.desk.portfolio.apply_flow(usd)
            self._event("funds.deposit", t.to_dict())
        else:
            t.note = "no matching deposit tag / linked sender - held for review, not credited"
            self.desk.bus.publish(
                "alert", Alert("warning", f"Unattributed {asset} deposit {tx_hash[:12]} held for review", "treasury"),
                source="treasury",
            )
        self._save_transfer(t)
        return t

    def credit_held(self, transfer_id: str, wallet_id: str) -> Transfer:
        t = next((x for x in self.transfers if x.id == transfer_id), None)
        if t is None or t.kind != "deposit" or t.status != "held":
            raise WalletError("no held deposit with that id")
        w = self.wallets.get(wallet_id)
        if w is None or w.chain != t.chain:
            raise WalletError("wallet must exist and be on the same chain")
        t.status, t.wallet_id = "completed", w.id
        t.note = f"manually attributed to {w.label}"
        if self.desk.is_live:
            t.note += " (live mode: recorded only)"
        else:
            self.desk.portfolio.apply_flow(t.usd_value)
            self._event("funds.deposit", t.to_dict())
        self.db.run("UPDATE transfers SET data = ? WHERE id = ?", (json.dumps(t.to_dict()), t.id))
        return t

    # -- withdrawals ------------------------------------------------------
    def withdrawn_today(self) -> float:
        start = time.time() - 86_400
        return sum(t.usd_value for t in self.transfers
                   if t.kind == "withdrawal" and t.ts >= start and t.status != "rejected")

    def withdraw(self, wallet_id: str, asset: str, amount: float) -> Transfer:
        s = self.desk.settings
        w = self.wallets.get(wallet_id)
        if w is None:
            raise WalletError("withdrawals can only go to a linked wallet")
        c = CHAINS[w.chain]
        asset = asset.strip().upper()
        if asset not in c.assets:
            raise WalletError(f"{asset} cannot be sent on {c.name}")
        if not (isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0):
            raise WalletError("amount must be a positive number")
        unlock = w.linked_at + s.withdrawal_cooldown_h * 3600
        if time.time() < unlock:
            hrs = (unlock - time.time()) / 3600
            raise WalletError(f"security cooldown: new wallet can receive withdrawals in {hrs:.1f}h")
        usd = round(amount * self.usd_price(asset), 2)
        if not w.verified and usd > s.test_send_usd:
            raise WalletError(f"The first send to a new wallet is a test of up to ${s.test_send_usd:,.0f}, so a wrong "
                              f"address can never cost much. Send a small amount, check it arrived, tap \"I got it\", "
                              f"and full amounts unlock.")
        fee = c.fee_usd
        if usd < 2 * fee:
            raise WalletError(f"too small to be worth sending: the transfer fee (about ${fee:,.2f}) would take over half "
                              "of it - send more at once")
        free = self.desk.free_cash()
        if usd > free + 1e-9:
            raise WalletError(f"insufficient free cash ({free:,.2f} USD available)")
        if self.withdrawn_today() + usd > s.max_daily_withdrawal:
            raise WalletError(f"daily withdrawal limit {s.max_daily_withdrawal:,.0f} USD would be exceeded")
        if self.desk.is_live and not self.desk.owner_protected():
            raise WalletError("log in to the website first (with the login code from your server logs), so only you can move real money")
        t = Transfer(f"wd_{secrets.token_hex(6)}", "withdrawal", w.chain, asset, amount, usd, "completed",
                     w.id, w.address, w.tag)
        if self.desk.is_live:
            t.status = "manual_required"
            t.note = (f"funds are held at your broker - complete this withdrawal there (not debited here); "
                      f"its transfer fee is about ${fee:,.2f}, so about ${max(0.0, usd - fee):,.2f} arrives")
        else:
            t.tx_hash = f"paper-{secrets.token_hex(8)}"
            t.note = f"paper withdrawal{f' with {c.tag.name} {w.tag}' if w.tag else ''}"
            self.desk.portfolio.apply_flow(-usd)
            self._event("funds.withdrawal", t.to_dict())
        self._save_transfer(t)
        return t

    def to_dict(self) -> dict:
        cd = self.desk.settings.withdrawal_cooldown_h
        return {
            "wallets": [w.to_dict(cd) for w in self.wallets.values()],
            "transfers": [t.to_dict() for t in self.transfers[-100:]][::-1],
            "deposit_info": self.deposit_info(),
            "chains": {k: {"name": c.name, "assets": list(c.assets), "tag": c.tag.name if c.tag else None, "fee": c.fee}
                       for k, c in CHAINS.items()},
            "withdrawn_today": self.withdrawn_today(),
            "max_daily_withdrawal": self.desk.settings.max_daily_withdrawal,
            "cooldown_h": cd,
            "test_send_usd": self.desk.settings.test_send_usd,
        }
