"""Envelope encryption for connector secrets and per-user OAuth token material.

Design (per plan Phase 0):
  - A key-encryption key (KEK) is supplied via ``APP_ENCRYPTION_KEY`` (env). In a
    production deployment this env value is itself sourced from a managed store
    (Azure Key Vault / HSM); here it is the single managed key.
  - Each encrypted value gets its own random 256-bit data-encryption key (DEK).
    The DEK encrypts the plaintext with AES-256-GCM; the KEK wraps the DEK with
    AES-256-GCM. Only the wrapped DEK + ciphertext + nonces/tags are persisted.
  - Authenticated Additional Data (AAD) is **recomputed by the caller** from
    immutable row identifiers (e.g. ``f"grant:{grant_id}"``) and bound into both
    the payload and the DEK-wrap. It is never stored, which prevents an attacker
    from swapping ciphertext between rows.
  - Key rotation: ``APP_ENCRYPTION_KEY`` may list multiple ``<kid>:<key>`` pairs
    (comma-separated). The first entry wraps new DEKs; every entry can unwrap
    existing ones. Each blob records the ``kek_id`` used.

Persist the fields of :class:`EncryptedBlob` (all short base64/text strings). No
plaintext, DEK, or KEK ever leaves this module.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:  # pragma: no cover - import guard
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _CRYPTO_IMPORT_OK = True
except Exception:  # noqa: BLE001
    AESGCM = None  # type: ignore[assignment]
    _CRYPTO_IMPORT_OK = False


ALGO = "AES-256-GCM"
_DEK_LEN = 32  # 256-bit
_NONCE_LEN = 12  # 96-bit, recommended for GCM


class CryptoError(RuntimeError):
    """Raised when encryption/decryption cannot be performed."""


@dataclass(frozen=True)
class EncryptedBlob:
    """A self-describing encrypted value. All fields are safe to store at rest."""

    algo: str
    kek_id: str
    ciphertext: str  # base64: AES-GCM(plaintext) — includes GCM tag
    nonce: str  # base64: payload nonce
    wrapped_dek: str  # base64: AES-GCM(dek) under the KEK — includes tag
    dek_nonce: str  # base64: nonce used to wrap the DEK

    def to_row(self) -> Dict[str, str]:
        return {
            "algo": self.algo,
            "kek_id": self.kek_id,
            "ciphertext": self.ciphertext,
            "nonce": self.nonce,
            "wrapped_dek": self.wrapped_dek,
            "dek_nonce": self.dek_nonce,
        }

    @classmethod
    def from_row(cls, row: Dict[str, object]) -> "EncryptedBlob":
        return cls(
            algo=str(row["algo"]),
            kek_id=str(row["kek_id"]),
            ciphertext=str(row["ciphertext"]),
            nonce=str(row["nonce"]),
            wrapped_dek=str(row["wrapped_dek"]),
            dek_nonce=str(row["dek_nonce"]),
        )


# ── KEK resolution ──────────────────────────────────────────────────────────

def _dev_mode() -> bool:
    # Default TRUE (POC/portable). Set JEEN_DEV_MODE=false to harden (then a
    # weak/passphrase APP_ENCRYPTION_KEY is rejected instead of derived).
    raw = os.getenv("JEEN_DEV_MODE")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on", "t")


def _decode_strong_key(raw: str) -> Optional[bytes]:
    """Return exactly 32 bytes for a strong key, or None if not a strong key.

    Accepts a base64 (std or url-safe) or hex encoding of 32 random bytes. A short
    or low-entropy passphrase is NOT a strong key and returns None.
    """
    raw = raw.strip()
    # base64 (standard)
    try:
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) == _DEK_LEN:
            return decoded
    except Exception:  # noqa: BLE001
        pass
    # base64 (url-safe, tolerate missing padding)
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == _DEK_LEN:
            return decoded
    except Exception:  # noqa: BLE001
        pass
    # hex
    try:
        if len(raw) == _DEK_LEN * 2:
            decoded = bytes.fromhex(raw)
            if len(decoded) == _DEK_LEN:
                return decoded
    except Exception:  # noqa: BLE001
        pass
    return None


def _load_keks() -> List[Tuple[str, bytes]]:
    """Return an ordered list of (kek_id, key_bytes). First = active wrapper.

    Weak/passphrase keys are rejected: entries that are not a 32-byte base64/hex
    key are skipped (with a logged error) so the feature fails closed. In
    JEEN_DEV_MODE only, a passphrase is derived to 32 bytes (with a warning) to
    keep local development frictionless.
    """
    raw = (os.getenv("APP_ENCRYPTION_KEY") or "").strip()
    if not raw:
        return []
    keks: List[Tuple[str, bytes]] = []
    for i, part in enumerate(p for p in raw.split(",") if p.strip()):
        part = part.strip()
        if ":" in part:
            kid, key = part.split(":", 1)
            kid = kid.strip() or f"k{i}"
        else:
            kid, key = ("k0" if i == 0 else f"k{i}"), part
        strong = _decode_strong_key(key)
        if strong is not None:
            keks.append((kid, strong))
        elif _dev_mode():
            import logging

            logging.getLogger(__name__).warning(
                "APP_ENCRYPTION_KEY entry %s is not a 32-byte base64 key; deriving "
                "one from the passphrase (DEV ONLY — never do this in production).", kid,
            )
            keks.append((kid, hashlib.sha256(key.encode("utf-8")).digest()))
        else:
            import logging

            logging.getLogger(__name__).error(
                "APP_ENCRYPTION_KEY entry %s is not a base64-encoded 32-byte key and "
                "is rejected (weak passphrases are not accepted in production). "
                "Generate one: python -c \"import base64,os; print(base64.b64encode(os.urandom(32)).decode())\"",
                kid,
            )
    return keks


def crypto_available() -> bool:
    """True when the cipher backend is importable and a strong KEK is configured."""
    return _CRYPTO_IMPORT_OK and bool(_load_keks())


def assert_kek_valid() -> None:
    """Startup check: if APP_ENCRYPTION_KEY is set it must yield a strong KEK.

    Raises CryptoError when a key is configured but none of its entries are a
    valid 32-byte key (outside dev mode). An unset key is allowed — the connector
    feature simply stays disabled/fail-closed.
    """
    configured = bool((os.getenv("APP_ENCRYPTION_KEY") or "").strip())
    if configured and not _load_keks():
        raise CryptoError(
            "APP_ENCRYPTION_KEY is set but is not a valid base64/hex 32-byte key. "
            "Refusing to start with a weak encryption key."
        )


def _active_kek() -> Tuple[str, bytes]:
    keks = _load_keks()
    if not keks:
        raise CryptoError(
            "APP_ENCRYPTION_KEY is not configured — refusing to handle connector "
            "secrets without envelope encryption."
        )
    if not _CRYPTO_IMPORT_OK:
        raise CryptoError(
            "The 'cryptography' package is not installed — cannot encrypt secrets."
        )
    return keks[0]


def _kek_by_id(kek_id: str) -> bytes:
    for kid, key in _load_keks():
        if kid == kek_id:
            return key
    raise CryptoError(f"No configured KEK matches kek_id={kek_id!r} (rotated/removed?).")


# ── Public API ──────────────────────────────────────────────────────────────

def encrypt(plaintext: str, *, aad: str) -> EncryptedBlob:
    """Envelope-encrypt ``plaintext`` binding it to ``aad`` (an immutable id).

    ``aad`` MUST be recomputed on decrypt from the same immutable row identifier
    so ciphertext cannot be moved between rows.
    """
    if plaintext is None:
        raise CryptoError("Cannot encrypt None")
    kek_id, kek = _active_kek()
    aad_b = aad.encode("utf-8")

    dek = os.urandom(_DEK_LEN)
    payload_nonce = os.urandom(_NONCE_LEN)
    dek_nonce = os.urandom(_NONCE_LEN)

    ct = AESGCM(dek).encrypt(payload_nonce, plaintext.encode("utf-8"), aad_b)
    wrapped = AESGCM(kek).encrypt(dek_nonce, dek, aad_b)

    return EncryptedBlob(
        algo=ALGO,
        kek_id=kek_id,
        ciphertext=_b64(ct),
        nonce=_b64(payload_nonce),
        wrapped_dek=_b64(wrapped),
        dek_nonce=_b64(dek_nonce),
    )


def decrypt(blob: EncryptedBlob, *, aad: str) -> str:
    """Decrypt an :class:`EncryptedBlob`, verifying it is bound to ``aad``."""
    if not _CRYPTO_IMPORT_OK:
        raise CryptoError("The 'cryptography' package is not installed.")
    if blob.algo != ALGO:
        raise CryptoError(f"Unsupported algo {blob.algo!r}")
    kek = _kek_by_id(blob.kek_id)
    aad_b = aad.encode("utf-8")
    try:
        dek = AESGCM(kek).decrypt(_unb64(blob.dek_nonce), _unb64(blob.wrapped_dek), aad_b)
        pt = AESGCM(dek).decrypt(_unb64(blob.nonce), _unb64(blob.ciphertext), aad_b)
    except Exception as exc:  # noqa: BLE001 - invalid tag / wrong aad / tampering
        raise CryptoError("Decryption failed (wrong key, tampered data, or AAD mismatch).") from exc
    return pt.decode("utf-8")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))
