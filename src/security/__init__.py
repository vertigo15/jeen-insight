"""Security primitives for the connector / integration platform.

- ``crypto``          — envelope encryption (AES-GCM) for secrets at rest.
- ``internal_auth``   — Flask-minted, FastAPI-verified service Principal tokens.
- ``app_flags``       — global admin master switch (``connectors_enabled``).
"""

from .crypto import (
    CryptoError,
    EncryptedBlob,
    crypto_available,
    decrypt,
    encrypt,
)
from .internal_auth import (
    Principal,
    PrincipalError,
    issue_internal_token,
    verify_internal_token,
)

__all__ = [
    "CryptoError",
    "EncryptedBlob",
    "crypto_available",
    "decrypt",
    "encrypt",
    "Principal",
    "PrincipalError",
    "issue_internal_token",
    "verify_internal_token",
]
