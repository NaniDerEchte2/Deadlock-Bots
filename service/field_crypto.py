"""
Field-level AES-256-GCM encryption for sensitive database fields.

Provides encryption/decryption with:
- AES-256-GCM (AEAD - Authenticated Encryption with Associated Data)
- Per-field unique nonces (never reused)
- AAD binding (prevents token copying between rows)
- Key rotation support via kid (key identifier)
- Storage format: version(1)|kid_len(1)|kid(var)|nonce(12)|ciphertext+tag

Usage:
    from service.field_crypto import get_crypto

    crypto = get_crypto()

    # Encrypt
    aad = "table_name|column_name|row_id|enc_version"
    encrypted = crypto.encrypt_field("sensitive_value", aad, kid="v1")

    # Decrypt
    plaintext = crypto.decrypt_field(encrypted, aad)
"""

from __future__ import annotations

import logging
import secrets
import struct
from typing import Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)


class CryptoError(Exception):
    """Base exception for crypto operations."""
    pass


class KeyMissing(CryptoError):
    """Encryption key not found."""
    pass


class DecryptFailed(CryptoError):
    """Decryption failed (wrong key, corrupted data, AAD mismatch)."""
    pass


class InvalidPayload(CryptoError):
    """Invalid encrypted payload format."""
    pass


class FieldCrypto:
    """AES-256-GCM field-level encryption."""

    VERSION = 1
    NONCE_SIZE = 12  # 96 bits (recommended for GCM)
    KEY_SIZE = 32    # 256 bits

    def __init__(self):
        """Initialize crypto with keys from Windows Credential Manager."""
        self._keys: Dict[str, bytes] = {}
        self._load_keys()

    def _load_keys(self) -> None:
        """Load encryption keys from Windows Credential Manager."""
        try:
            import keyring
        except ImportError:
            raise KeyMissing(
                "keyring package not installed. Install with: pip install keyring"
            )

        # Load v1 master key
        key_v1 = keyring.get_password("DeadlockBot", "DB_MASTER_KEY_V1")

        if not key_v1:
            raise KeyMissing(
                "DB_MASTER_KEY_V1 not found in Windows Credential Manager. "
                "Run scripts/generate_master_key.py to generate and store the key."
            )

        try:
            self._keys["v1"] = bytes.fromhex(key_v1)
            log.info("Loaded encryption key: v1")
        except ValueError as e:
            raise KeyMissing(f"Invalid key format for v1: {e}")

        # Validate key size
        if len(self._keys["v1"]) != self.KEY_SIZE:
            raise KeyMissing(
                f"Key v1 has invalid size: {len(self._keys['v1'])} bytes "
                f"(expected {self.KEY_SIZE})"
            )

    def encrypt_field(
        self,
        plaintext: str,
        aad: str,
        kid: str = "v1"
    ) -> bytes:
        """
        Encrypt a field value.

        Args:
            plaintext: Value to encrypt
            aad: Associated data (table|column|row_id|version)
                 Used to bind encryption to specific context.
                 Example: "twitch_raid_auth|access_token|123456|1"
            kid: Key identifier (default: "v1")

        Returns:
            BLOB: version(1) + kid_len(1) + kid(var) + nonce(12) + ciphertext+tag

        Raises:
            KeyMissing: If specified key not found
        """
        if kid not in self._keys:
            raise KeyMissing(f"Encryption key '{kid}' not found")

        key = self._keys[kid]

        # Generate unique nonce (CRITICAL: never reuse)
        nonce = secrets.token_bytes(self.NONCE_SIZE)

        # Encrypt with AAD binding
        aesgcm = AESGCM(key)
        try:
            ciphertext = aesgcm.encrypt(
                nonce,
                plaintext.encode('utf-8'),
                aad.encode('utf-8')
            )
        except Exception as e:
            log.error("Encryption failed: %s", e)
            raise CryptoError(f"Encryption failed: {e}")

        # Pack: version(1) + kid_len(1) + kid(var) + nonce(12) + ciphertext+tag
        kid_bytes = kid.encode('ascii')
        kid_len = len(kid_bytes)

        if kid_len > 255:
            raise ValueError("Key ID too long (max 255 bytes)")

        blob = struct.pack('BB', self.VERSION, kid_len) + kid_bytes + nonce + ciphertext

        log.debug(
            "Encrypted field: kid=%s, aad=%s, size=%d bytes",
            kid, aad, len(blob)
        )

        return blob

    def decrypt_field(
        self,
        blob: bytes,
        aad: str
    ) -> str:
        """
        Decrypt a field value.

        Args:
            blob: Encrypted blob
            aad: Associated data (must match encryption AAD)

        Returns:
            Decrypted plaintext

        Raises:
            InvalidPayload: If blob format is invalid
            KeyMissing: If decryption key not found
            DecryptFailed: If decryption fails (wrong key, corrupted data, AAD mismatch)
        """
        if not blob:
            raise InvalidPayload("Empty blob")

        if len(blob) < 15:  # min size: version(1) + kid_len(1) + kid(1) + nonce(12)
            raise InvalidPayload(f"Blob too short: {len(blob)} bytes")

        # Unpack header
        version, kid_len = struct.unpack('BB', blob[:2])

        if version != self.VERSION:
            raise InvalidPayload(f"Unknown version: {version} (expected {self.VERSION})")

        # Extract kid
        kid_start = 2
        kid_end = kid_start + kid_len

        if len(blob) < kid_end + self.NONCE_SIZE:
            raise InvalidPayload("Blob truncated (missing nonce)")

        try:
            kid = blob[kid_start:kid_end].decode('ascii')
        except UnicodeDecodeError as e:
            raise InvalidPayload(f"Invalid key ID encoding: {e}")

        # Extract nonce and ciphertext
        nonce_start = kid_end
        nonce_end = nonce_start + self.NONCE_SIZE
        nonce = blob[nonce_start:nonce_end]
        ciphertext = blob[nonce_end:]

        if not ciphertext:
            raise InvalidPayload("Blob truncated (missing ciphertext)")

        # Get decryption key
        if kid not in self._keys:
            raise KeyMissing(f"Decryption key '{kid}' not found")

        key = self._keys[kid]

        # Decrypt with AAD verification
        aesgcm = AESGCM(key)
        try:
            plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, aad.encode('utf-8'))
            plaintext = plaintext_bytes.decode('utf-8')
        except Exception as e:
            log.error("Decryption failed for kid=%s, aad=%s: %s", kid, aad, e)
            raise DecryptFailed(f"Decryption failed: {e}")

        log.debug("Decrypted field: kid=%s, aad=%s", kid, aad)

        return plaintext


# Singleton instance
_crypto: Optional[FieldCrypto] = None


def get_crypto() -> FieldCrypto:
    """
    Get singleton FieldCrypto instance.

    Returns:
        FieldCrypto instance

    Raises:
        KeyMissing: If master key not found
    """
    global _crypto
    if _crypto is None:
        _crypto = FieldCrypto()
    return _crypto


def reset_crypto() -> None:
    """
    Reset singleton (for testing or key rotation).

    Forces reload of keys on next get_crypto() call.
    """
    global _crypto
    _crypto = None
    log.info("Crypto singleton reset")
