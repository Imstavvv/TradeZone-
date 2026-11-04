import asyncio

import pytest

from tradezone.wallets import WalletError

XRP = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
ETH = "0x52908400098527886E0F7030069857D2E4169EE7"
BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


@pytest.fixture
def w(desk):
    desk.settings.withdrawal_cooldown_h = 0
    desk.settings.test_send_usd = 1e9  # these tests check the other rules; the test-send rule has its own
    desk.prices["BTC-USD"] = 50_000.0
    return desk.wallets


def test_link_validates_address_and_tag(w):
    with pytest.raises(WalletError, match="isn't a valid"):
        w.link("ETH", "0x123")
    with pytest.raises(WalletError, match="destination tag"):
        w.link("XRP", XRP)  # tag missing and not confirmed
    with pytest.raises(WalletError, match="integer"):
        w.link("XRP", XRP, tag="abc")
    with pytest.raises(WalletError, match="does not use"):
        w.link("ETH", ETH, tag="123")
    wallet = w.link("XRP", XRP, tag="123456", label="Exchange")
    assert wallet.tag == "123456" and wallet.deposit_tag.isdigit()
    assert w.link("XRP", XRP, no_tag_confirmed=True).tag is None
    with pytest.raises(WalletError, match="already linked"):
        w.link("XRP", XRP, tag="123456")
    assert w.link("BTC", BTC).chain == "BTC"


def test_deposit_matched_by_tag_credits_cash(desk, w):
    wallet = w.link("XRP", XRP, tag="1", label="x")
    desk.prices["XRP-USD"] = 0.5
    cash0, start0 = desk.portfolio.cash, desk.portfolio.starting_cash
    t = w.record_deposit("XRP", "XRP", 1000, "tx1", tag=wallet.deposit_tag)
    assert t.status == "completed" and t.wallet_id == wallet.id
    assert desk.portfolio.cash == pytest.approx(cash0 + 500)
    assert desk.portfolio.starting_cash == pytest.approx(start0 + 500)  # funding isn't P&L
    with pytest.raises(WalletError, match="already recorded"):
        w.record_deposit("XRP", "XRP", 1000, "tx1", tag=wallet.deposit_tag)


def test_unmatched_deposit_is_held_then_credited(desk, w):
    wallet = w.link("ETH", ETH)
    cash0 = desk.portfolio.cash
    t = w.record_deposit("ETH", "USDC", 250, "tx2", from_address="0x" + "1" * 40)
    assert t.status == "held" and desk.portfolio.cash == cash0
    w.credit_held(t.id, wallet.id)
    assert desk.portfolio.cash == cash0 + 250
    t2 = w.record_deposit("ETH", "USDC", 10, "tx3", from_address=ETH.lower())
    assert t2.status == "completed"


def test_withdrawal_rules(desk, w):
    wallet = w.link("XRP", XRP, tag="777")
    desk.prices["XRP-USD"] = 1.0
    t = w.withdraw(wallet.id, "XRP", 100)
    assert t.status == "completed" and t.tag == "777" and t.address == XRP
    with pytest.raises(WalletError, match="cannot be sent"):
        w.withdraw(wallet.id, "BTC", 1)
    with pytest.raises(WalletError, match="linked wallet"):
        w.withdraw("w_nope", "XRP", 1)
    with pytest.raises(WalletError, match="insufficient"):
        w.withdraw(wallet.id, "XRP", 10_000_000)
    with pytest.raises(WalletError, match="daily withdrawal limit"):
        w.withdraw(wallet.id, "XRP", 9_950)
    desk.settings.withdrawal_cooldown_h = 24
    fresh = w.link("BTC", BTC)
    with pytest.raises(WalletError, match="cooldown"):
        w.withdraw(fresh.id, "BTC", 0.01)


def test_funding_keeps_audit_balanced(desk, w):
    wallet = w.link("ETH", ETH)
    w.record_deposit("ETH", "USDC", 1000, "tx9", from_address=ETH)
    w.withdraw(wallet.id, "USDC", 400)
    asyncio.run(desk.bus.drain())
    sen = desk.bots["sentinel"]
    asyncio.run(sen.on_cycle())
    assert sen.books_balanced and not desk.paused


def test_wallets_persist(desk, w):
    from tradezone.desk import TradingDesk

    w.link("ETH", ETH, label="cold")
    again = TradingDesk(desk.settings, journal=desk.journal)
    assert [x.label for x in again.wallets.wallets.values()] == ["cold"]


def test_wallet_api(desk):
    from fastapi.testclient import TestClient

    from tradezone.server import create_app

    desk.settings.withdrawal_cooldown_h = 0
    with TestClient(create_app(desk, run_desk=False)) as c:
        r = c.post("/api/wallets", json={"chain": "XRP", "address": XRP, "tag": "42"})
        assert r.status_code == 200, r.text
        wid = r.json()["id"]
        assert c.post("/api/wallets", json={"chain": "XRP", "address": "bad"}).status_code == 400
        dtag = c.get("/api/deposits/info").json()[0]["deposit_tag"]
        desk.prices["XRP-USD"] = 2.0
        assert c.post("/api/deposits", json={"chain": "XRP", "asset": "XRP", "amount": 50, "tx_hash": "h",
                                             "tag": dtag}).json()["usd_value"] == 100
        assert c.post("/api/withdrawals", json={"wallet_id": wid, "asset": "XRP", "amount": 10}).json()["tag"] == "42"
        assert len(c.get("/api/wallets").json()["transfers"]) == 2
        assert c.delete(f"/api/wallets/{wid}").json()["ok"]
        assert c.post("/api/grok/ping").json()["ok"] is False  # no key configured


def test_typo_in_an_address_is_caught_by_its_checksum(w):
    with pytest.raises(WalletError, match="checksum"):
        w.link("BTC", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdx")  # last character wrong
    with pytest.raises(WalletError, match="checksum"):
        w.link("XRP", "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYf", no_tag_confirmed=True)
    with pytest.raises(WalletError, match="checksum"):
        w.link("ETH", "0x52908400098527886E0F7030069857D2E4169Ee7")  # wrong capital letter
    assert w.link("ETH", "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed").checksum == "verified"
    assert w.link("SOL", "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV").checksum == "none"  # Solana has none


def test_more_networks_and_detection(w):
    found = {c: [n["chain"] for n in w.check(a)["networks"]] for c, a in {
        "LTC": "LUWPbpM43E2p7ZSh8cyTBEkvpHmr3cB8Ez", "DOGE": "DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L",
        "TRX": "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy", "DOT": "15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5",
        "ADA": "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer3n0d3vllmyqwsx5wktcd8cc3sq835lu7drv2xwl2wywfgse35a3x",
        "ALGO": "VCMJKWOY5P5P7SKMZFFOCEROPJCZOTIJMNIYNUCKH7LRO45JMJP6UYBIJA"}.items()}
    assert all(found[c] == [c] for c in found), found
    evm = [n["chain"] for n in w.check("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")["networks"]]
    assert {"ETH", "BASE", "ARB", "POLYGON", "BSC", "AVAXC", "OP"} <= set(evm)  # you pick the network
    assert w.check("bitcoin:bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq?amount=0.1")["networks"][0]["chain"] == "BTC"
    assert w.check("not an address")["networks"] == []
    assert w.link("BASE", "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", label="USDC on Base").to_dict()["fee"] == "a few cents"


def test_first_send_to_a_new_wallet_is_a_small_test(desk, w):
    desk.settings.test_send_usd = 25.0
    wallet = w.link("XRP", XRP, tag="77")
    desk.prices["XRP-USD"] = 1.0
    with pytest.raises(WalletError, match="first send to a new wallet is a test"):
        w.withdraw(wallet.id, "XRP", 100)
    with pytest.raises(WalletError, match="send a small test amount"):
        w.confirm_received(wallet.id)  # nothing sent yet
    assert w.withdraw(wallet.id, "XRP", 20).status == "completed"
    assert w.confirm_received(wallet.id).verified
    assert w.withdraw(wallet.id, "XRP", 100).status == "completed"  # full amounts now
    reloaded = type(w)(desk)
    assert reloaded.wallets[wallet.id].verified  # remembered
