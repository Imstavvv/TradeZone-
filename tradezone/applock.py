"""App lock: the owner's password (an old 6-digit PIN still works) and/or Face ID passkeys.

Model:

1. A device logs in once with the password or Face ID and then stays logged in (30-day sliding
   session). The phone's password manager (iCloud Keychain, Google) can save the password and
   fill it in with Face ID or a fingerprint, so logging in is usually one tap.
2. Optionally (``lock_after_min`` > 0) the app re-locks after that many idle minutes. By default
   it doesn't: "Lock now" in Settings locks it on demand.
3. Too many wrong passwords sign the device out, and logins on new devices are rate-limited.

Face ID uses the WebAuthn passkey standard with *user verification required*. The phone's secure
hardware checks the face and signs a fresh challenge. No biometric data ever leaves the device;
the server only stores a public key. The same flow covers Touch ID, Android fingerprint or face
unlock, and Windows Hello.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from typing import Any

PIN_LEN = 6
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128
MAX_PIN_FAILURES = 8
NEVER = 10 * 365 * 24 * 3600.0  # "unlocked until" when auto-lock is off
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
WEAK_PASSWORDS = {"password", "password1", "qwerty", "qwerty123", "abc123", "abcdef", "letmein", "iloveyou",
                  "tradezone", "trading", "bitcoin", "monkey", "dragon", "welcome", "admin1", "111111"}
WEAK_PINS = {
    "123456", "654321", "123123", "121212", "112233", "111222", "123321", "696969", "101010", "159753",
    "147258", "258369", "000001", "100000", "202020", "102030", "246810", "135790", "999999", "888888",
}
USER_ID = b"tradezone-owner"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_lock (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passkeys (
    id TEXT PRIMARY KEY, public_key TEXT NOT NULL, sign_count INTEGER NOT NULL, label TEXT,
    rp_id TEXT, created REAL, last_used REAL
);
"""


class LockError(ValueError):
    pass


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_SMART = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
                        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
                        "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " "})


def normalize_password(pw: str) -> str:
    """The same password however the keyboard typed it.

    Phones turn ' into ’ and -- into — in some fields (for example after tapping "Show"), and
    letters can arrive in different Unicode forms. Without this, a password saved one way
    wouldn't match the same keys typed another way.
    """
    return unicodedata.normalize("NFKC", pw or "").translate(_SMART).strip()


def weak_pin_reason(pin: str) -> str | None:
    """Why a password (or all-digit PIN) is too weak, or None if it's fine."""
    if len(pin) < MIN_PASSWORD_LEN:
        return f"use at least {MIN_PASSWORD_LEN} characters"
    if len(pin) > MAX_PASSWORD_LEN:
        return f"use at most {MAX_PASSWORD_LEN} characters"
    if pin.lower() in WEAK_PASSWORDS:
        return "that password is too common"
    if not pin.isdigit():
        return None if len(set(pin)) >= 3 else "use at least 3 different characters"
    if len(set(pin)) == 1:
        return "all digits are the same"
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if steps in ({1}, {-1}):
        return "the digits are in sequence"
    if pin in WEAK_PINS or pin[:3] == pin[3:] or pin[:2] * 3 == pin:
        return "that is too common or repetitive"
    if len(set(pin)) <= 2:
        return "use at least 3 different digits"
    return None


class AppLock:
    def __init__(self, journal, lock_after_min: float = 5.0) -> None:
        self.journal = journal
        # 0 = never lock by itself (stay logged in); otherwise re-lock after this much idle time
        self.lock_after_s = NEVER if lock_after_min <= 0 else max(30.0, lock_after_min * 60)
        self._challenges: dict[str, tuple[bytes, float, str]] = {}  # session hash -> (challenge, expiry, kind)
        with journal._lock:
            journal.conn.executescript(SCHEMA)
            cols = {r[1] for r in journal.conn.execute("PRAGMA table_info(sessions)")}
            if "unlocked_until" not in cols:
                journal.conn.execute("ALTER TABLE sessions ADD COLUMN unlocked_until REAL DEFAULT 0")
            if "pin_failures" not in cols:
                journal.conn.execute("ALTER TABLE sessions ADD COLUMN pin_failures INTEGER DEFAULT 0")
            journal.conn.commit()

    # ------------------------------------------------------------ storage
    def _get(self, key: str) -> dict | None:
        row = self.journal.one("SELECT data FROM app_lock WHERE key = ?", (key,))
        return json.loads(row[0]) if row else None

    def _put(self, key: str, data: dict | None) -> None:
        with self.journal._lock:
            if data is None:
                self.journal.conn.execute("DELETE FROM app_lock WHERE key = ?", (key,))
            else:
                self.journal.conn.execute("INSERT OR REPLACE INTO app_lock(key, data) VALUES (?,?)",
                                          (key, json.dumps(data)))
            self.journal.conn.commit()

    @property
    def lock_after_min(self) -> float:
        """Idle minutes before re-locking (0 = never)."""
        return 0.0 if self.lock_after_s >= NEVER else self.lock_after_s / 60

    # ---------------------------------------------------------------- PIN
    @property
    def pin_set(self) -> bool:
        return self._get("pin") is not None

    def set_pin(self, pin: str) -> None:
        pin = normalize_password(pin)
        if (why := weak_pin_reason(pin)) is not None:
            raise LockError(f"Choose a stronger password: {why}.")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(pin.encode(), salt=salt, **SCRYPT)
        self._put("pin", {"salt": b64u(salt), "hash": b64u(digest), "set_at": time.time()})

    def remove_pin(self) -> None:
        self._put("pin", None)

    def check_pin(self, pin: str) -> bool:
        rec = self._get("pin")
        if not rec or not pin or len(pin) > MAX_PASSWORD_LEN:
            return False
        salt, want = unb64u(rec["salt"]), unb64u(rec["hash"])
        # normalised form first; the raw form too, for passwords saved before normalising existed
        for cand in dict.fromkeys((normalize_password(pin), pin.strip())):
            if cand and hmac.compare_digest(hashlib.scrypt(cand.encode(), salt=salt, **SCRYPT), want):
                return True
        return False

    # ----------------------------------------------------------- passkeys
    def passkeys(self) -> list[dict[str, Any]]:
        rows = self.journal.all("SELECT id, label, created, last_used, rp_id FROM passkeys ORDER BY created")
        return [{"id": r[0], "label": r[1], "created": r[2], "last_used": r[3], "rp_id": r[4]} for r in rows]

    def remove_passkey(self, cred_id: str) -> bool:
        n = self.journal.run("DELETE FROM passkeys WHERE id = ?", (cred_id,))
        return n > 0

    @property
    def enabled(self) -> bool:
        return self.pin_set or bool(self.passkeys())

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "pin": self.pin_set, "passkeys": self.passkeys(),
                "lock_after_min": self.lock_after_min, "max_pin_failures": MAX_PIN_FAILURES}

    # ---------------------------------------------------- session lock state
    def is_unlocked(self, sid_hash: str) -> bool:
        if not self.enabled:
            return True
        row = self.journal.one("SELECT unlocked_until FROM sessions WHERE id_hash = ?", (sid_hash,))
        until, now = (row[0] or 0) if row else 0, time.time()
        if self.lock_after_s < NEVER:  # auto-lock was switched on later: old "never" unlocks don't count
            until = min(until, now + self.lock_after_s) if until - now <= self.lock_after_s + 60 else 0
        return until > now

    def unlock(self, sid_hash: str) -> None:
        self.journal.run(
            "UPDATE sessions SET unlocked_until = ?, pin_failures = 0 WHERE id_hash = ?",
            (time.time() + self.lock_after_s, sid_hash),
        )

    def touch(self, sid_hash: str) -> None:
        """Activity keeps an unlocked session unlocked (sliding window, written at most every 20s)."""
        if self.lock_after_s >= NEVER:
            return
        now = time.time()
        with self.journal._lock:
            row = self.journal.conn.execute(
                "SELECT unlocked_until FROM sessions WHERE id_hash = ?", (sid_hash,)).fetchone()
            if row and (row[0] or 0) > now and (row[0] - now) < self.lock_after_s - 20:
                self.journal.conn.execute("UPDATE sessions SET unlocked_until = ? WHERE id_hash = ?",
                                          (now + self.lock_after_s, sid_hash))
                self.journal.conn.commit()

    def lock(self, sid_hash: str) -> None:
        self.journal.run("UPDATE sessions SET unlocked_until = 0 WHERE id_hash = ?", (sid_hash,))

    def pin_failed(self, sid_hash: str) -> int:
        """Count a wrong PIN; returns attempts left (0 means the session was revoked)."""
        with self.journal._lock:
            self.journal.conn.execute(
                "UPDATE sessions SET pin_failures = COALESCE(pin_failures, 0) + 1 WHERE id_hash = ?", (sid_hash,))
            row = self.journal.conn.execute(
                "SELECT pin_failures FROM sessions WHERE id_hash = ?", (sid_hash,)).fetchone()
            n = row[0] if row else MAX_PIN_FAILURES
            if n >= MAX_PIN_FAILURES:
                self.journal.conn.execute("DELETE FROM sessions WHERE id_hash = ?", (sid_hash,))
            self.journal.conn.commit()
        return max(0, MAX_PIN_FAILURES - n)

    # ------------------------------------------------------------ WebAuthn
    def _challenge(self, sid_hash: str, kind: str) -> bytes:
        now = time.time()
        self._challenges = {k: v for k, v in self._challenges.items() if v[1] > now}
        ch = secrets.token_bytes(32)
        self._challenges[sid_hash] = (ch, now + 120, kind)
        return ch

    def _take_challenge(self, sid_hash: str, kind: str) -> bytes:
        ch, exp, k = self._challenges.pop(sid_hash, (b"", 0.0, ""))
        if not ch or exp < time.time() or k != kind:
            raise LockError("Face ID request expired - please try again")
        return ch

    def registration_options(self, sid_hash: str, rp_id: str) -> str:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorAttachment,
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
        opts = generate_registration_options(
            rp_id=rp_id, rp_name="TradeZone", user_id=USER_ID, user_name="TradeZone owner",
            user_display_name="TradeZone owner", challenge=self._challenge(sid_hash, "reg"),
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,  # the phone's own Face ID / Touch ID
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=unb64u(p["id"])) for p in self.passkeys()],
        )
        return options_to_json(opts)

    def register(self, sid_hash: str, credential: dict, rp_id: str, origin: str, label: str) -> dict:
        from webauthn import verify_registration_response
        ch = self._take_challenge(sid_hash, "reg")
        try:
            v = verify_registration_response(credential=credential, expected_challenge=ch, expected_rp_id=rp_id,
                                             expected_origin=origin, require_user_verification=True)
        except Exception as exc:
            raise LockError(f"Face ID setup could not be verified: {exc}") from None
        cid, now = b64u(v.credential_id), time.time()
        self.journal.run(
            "INSERT OR REPLACE INTO passkeys(id, public_key, sign_count, label, rp_id, created, last_used) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, b64u(v.credential_public_key), v.sign_count, (label or "Face ID")[:60], rp_id, now, now),
        )
        return {"id": cid, "label": label}

    def authentication_options(self, sid_hash: str, rp_id: str) -> str:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import (
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )
        keys = [p for p in self.passkeys() if p["rp_id"] == rp_id]
        if not keys:
            raise LockError("Face ID isn't set up for this site")
        opts = generate_authentication_options(
            rp_id=rp_id, challenge=self._challenge(sid_hash, "auth"),
            allow_credentials=[PublicKeyCredentialDescriptor(id=unb64u(p["id"])) for p in keys],
            user_verification=UserVerificationRequirement.REQUIRED)
        return options_to_json(opts)

    def authenticate(self, sid_hash: str, credential: dict, rp_id: str, origin: str) -> str:
        from webauthn import verify_authentication_response
        ch = self._take_challenge(sid_hash, "auth")
        cid = credential.get("id") or credential.get("rawId") or ""
        row = self.journal.one("SELECT public_key, sign_count, label FROM passkeys WHERE id = ?", (cid,))
        if not row:
            raise LockError("unknown Face ID key - it may have been removed")
        try:
            v = verify_authentication_response(
                credential=credential, expected_challenge=ch, expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=unb64u(row[0]), credential_current_sign_count=row[1],
                require_user_verification=True)
        except Exception as exc:
            raise LockError(f"Face ID check failed: {exc}") from None
        self.journal.run(
            "UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?",
            (v.new_sign_count, time.time(), cid),
        )
        return row[2] or "Face ID"
