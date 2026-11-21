"""Muse's vault: application-level encryption of everything personal it stores.

Principle: real identifiers and content only exist on disk inside ciphertext. A database dump,
a backup or a copied volume without the key file reveals nothing: not what anyone watched,
not what they searched for, not even which titles are in the library.

* **seal / open**: AES-256-GCM (authenticated) with a random 96-bit nonce per value and the
  column's purpose as associated data, so a ciphertext can't be moved to another column or
  table and still decrypt. Token: `m1.<kid>.<base64url(nonce|ciphertext|tag)>`.
* **bid** (blind index): HMAC-SHA256 under a separate index key, for columns that must stay
  queryable (joins, lookups, dedupe): equal inputs give equal outputs, nothing else is learnt.
  Namespaced by purpose, so the same value gives different indexes in different places.
* **Key ring**: one key file, one key per line (`idx:` = the index key, which never rotates;
  `k<n>:` = encryption keys, the first one encrypts, all of them decrypt). Rotation adds a
  key on top and re-encrypts in the background (`muse rotate-keys`).

The key file lives at MUSE_MASTER_KEY_FILE (default /run/secrets/muse_master_key, a Docker
secret only the services that need it get; adapters never do). Without a key every service
refuses to start, unless MUSE_ENCRYPTION=off (development/tests only).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from functools import lru_cache
from typing import Any

DEFAULT_KEY_FILE = "/run/secrets/muse_master_key"
TOKEN_PREFIX = "m1."
BLIND_PREFIX = "b1"


class VaultUnavailable(RuntimeError):
    pass


class DecryptionError(ValueError):
    pass


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def generate_key_file() -> str:
    """A fresh key file: one index key and one encryption key."""
    return (
        "# Muse master key file. Keep it secret and back it up separately from your data:\n"
        "# without it the stored data cannot be read, by anyone, including you.\n"
        f"idx:{base64.b64encode(secrets.token_bytes(32)).decode()}\n"
        f"k1:{base64.b64encode(secrets.token_bytes(32)).decode()}\n"
    )


def add_key(key_file_text: str) -> tuple[str, str]:
    """Returns (new key file text with a new active encryption key on top, new kid)."""
    lines = [l for l in key_file_text.splitlines() if l.strip()]
    kids = [l.split(":", 1)[0] for l in lines if not l.startswith("#") and ":" in l]
    n = max([int(k[1:]) for k in kids if k.startswith("k") and k[1:].isdigit()] or [0]) + 1
    new = f"k{n}:{base64.b64encode(secrets.token_bytes(32)).decode()}"
    comments = [l for l in lines if l.startswith("#")]
    idx = [l for l in lines if l.startswith("idx:")]
    rest = [l for l in lines if not l.startswith("#") and not l.startswith("idx:")]
    return "\n".join(comments + idx + [new] + rest) + "\n", f"k{n}"


class Vault:
    enabled = True

    def __init__(self, key_file_text: str):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._enc: dict[str, Any] = {}
        self._order: list[str] = []
        idx: bytes | None = None
        for raw in key_file_text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            kid, _, b64 = line.partition(":")
            key = base64.b64decode(b64.strip())
            if len(key) != 32:
                raise VaultUnavailable(f"key {kid!r} must be 32 bytes")
            if kid == "idx":
                idx = key
            else:
                self._enc[kid] = AESGCM(key)
                self._order.append(kid)
        if idx is None or not self._order:
            raise VaultUnavailable("key file needs an `idx:` line and at least one `k<n>:` line")
        self._idx = idx
        self.active_kid = self._order[0]

    # ---- encryption ---------------------------------------------------------------------
    def seal(self, value: Any, purpose: str) -> str:
        """Any JSON-serialisable value -> token."""
        nonce = secrets.token_bytes(12)
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        ct = self._enc[self.active_kid].encrypt(nonce, data, purpose.encode())
        return f"{TOKEN_PREFIX}{self.active_kid}.{_b64e(nonce + ct)}"

    def open(self, token: Any, purpose: str) -> Any:
        """Token -> value. Non-tokens pass through unchanged (rows written before encryption
        was enabled are migrated by `muse migrate`; until then they stay readable)."""
        if not is_token(token):
            return token
        try:
            _, kid, body = token.split(".", 2)
            raw = _b64d(body)
            pt = self._enc[kid].decrypt(raw[:12], raw[12:], purpose.encode())
        except KeyError as exc:
            raise DecryptionError("unknown key id in token (was a key removed from the key file?)") from exc
        except Exception as exc:  # InvalidTag: wrong key, wrong purpose or tampered data
            raise DecryptionError(f"cannot decrypt {purpose} value") from exc
        return json.loads(pt)

    def seal_json(self, value: Any, purpose: str) -> dict:
        return {"_enc": self.seal(value, purpose)}

    def open_json(self, value: Any, purpose: str) -> Any:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return value
        if isinstance(value, dict) and set(value) <= {"_enc", "dedupe_key"} and "_enc" in value:
            return self.open(value["_enc"], purpose)
        return value

    def needs_reseal(self, token: Any) -> bool:
        return is_token(token) and not token.startswith(f"{TOKEN_PREFIX}{self.active_kid}.")

    def digest(self, text: str, purpose: str) -> str:
        """Keyed change-detection hash (a plain hash of public text could be matched by
        recomputing it from public data)."""
        return hmac.new(self._idx, f"{purpose}\x00{text}".encode(), hashlib.sha256).hexdigest()[:32]

    # ---- blind indexes ------------------------------------------------------------------
    def bid(self, value: str | None, purpose: str) -> str | None:
        if value is None:
            return None
        if is_blind(value):
            return value  # already an index (idempotent)
        mac = hmac.new(self._idx, f"{purpose}\x00{value}".encode(), hashlib.sha256).digest()
        return BLIND_PREFIX + base64.b32encode(mac[:20]).decode().lower().rstrip("=")


class NullVault:
    """MUSE_ENCRYPTION=off: plaintext, for development and tests only."""

    enabled = False
    active_kid = "none"

    def seal(self, value: Any, purpose: str) -> Any:
        return value

    def open(self, token: Any, purpose: str) -> Any:
        return token

    def seal_json(self, value: Any, purpose: str) -> Any:
        return value

    def open_json(self, value: Any, purpose: str) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def needs_reseal(self, token: Any) -> bool:
        return False

    def digest(self, text: str, purpose: str) -> str:
        return hashlib.sha256(f"{purpose}\x00{text}".encode()).hexdigest()[:32]

    def bid(self, value: str | None, purpose: str) -> str | None:
        return value


def is_token(v: Any) -> bool:
    return isinstance(v, str) and v.startswith(TOKEN_PREFIX)


def is_blind(v: Any) -> bool:
    return isinstance(v, str) and v.startswith(BLIND_PREFIX) and len(v) == 34


def load_key_text() -> str | None:
    inline = os.environ.get("MUSE_MASTER_KEY")
    if inline:
        return inline.replace("\\n", "\n")
    path = os.environ.get("MUSE_MASTER_KEY_FILE", DEFAULT_KEY_FILE)
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


@lru_cache(maxsize=1)
def get_vault() -> Vault | NullVault:
    if os.environ.get("MUSE_ENCRYPTION", "on").lower() in ("off", "false", "0"):
        return NullVault()
    text = load_key_text()
    if not text:
        raise VaultUnavailable(
            "no master key: mount the muse_master_key secret (./muse setup creates it) or set MUSE_ENCRYPTION=off for development"
        )
    return Vault(text)


def reset_vault_cache() -> None:
    get_vault.cache_clear()


# Purposes (associated data / index namespaces). One per column, never reused.
P_EVENT_METADATA = "event.metadata"
P_EVENT_ITEM = "event.source_item_id"
P_DEDUPE = "event.dedupe_key"
P_ITEM_KEY = "item.key"
P_ITEM_DATA = "item.data"
P_CANON = "canon.key"
P_CANON_DATA = "canon.data"
P_PAYLOAD = "qdrant.payload"
P_PROFILE = "taste_profile.profile"
P_QUESTION = "question.payload"
P_ANSWER = "question.answer"
P_IMPRESSION_ITEM = "impression.item"
P_OVERLAY = "user_overlay.pinned_topics"
P_DOWNLOAD_ITEM = "curator_download_log.item"
P_IDENTITY = "identity_mapping.username"


def event_item_bid(vault: Vault | NullVault, source: str, source_item_id: str | None) -> str | None:
    """The stored form of an event's source_item_id. Every writer (ingestion API, worker,
    recommendation API) must use exactly this, so item rows and events join."""
    if source_item_id is None:
        return None
    return vault.bid(f"{source}:{source_item_id}", P_EVENT_ITEM)


def item_key(vault: Vault | NullVault, source: str, source_item_id: str) -> str:
    """item.item_key for an event's (source, real source_item_id)."""
    return f"{source}:{event_item_bid(vault, source, source_item_id)}"
