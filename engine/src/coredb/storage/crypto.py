"""Pluggable encryption/decryption hooks for object storage.

Kept strictly decoupled from indexing structures so indexes operate on
record identifiers and search keys without coupling to cipher mechanics.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..errors import CryptoError


class EncryptionHook(ABC):
    """Interface invoked before persisting an object and after reading it back."""

    hook_name: str = "base"

    @abstractmethod
    def encrypt(self, plaintext: bytes, context: Optional[bytes] = None) -> bytes:
        """Transform plaintext object bytes into ciphertext bytes."""

    @abstractmethod
    def decrypt(self, ciphertext: bytes, context: Optional[bytes] = None) -> bytes:
        """Restore plaintext bytes from ciphertext bytes (raising CryptoError on failure)."""


class NoOpEncryptionHook(EncryptionHook):
    """Identity hook used when object encryption is disabled."""

    hook_name = "noop"

    def encrypt(self, plaintext: bytes, context: Optional[bytes] = None) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes, context: Optional[bytes] = None) -> bytes:
        return ciphertext


class AuthenticatedStreamHook(EncryptionHook):
    """Authenticated symmetric encryption hook (SHA-256 CTR + HMAC-SHA256).

    Wire format:
      [ iv (16B) | ciphertext (N bytes) | mac_tag (32B) ]

    Uses only Python's standard library so tests and benchmarks run anywhere
    without native C dependencies while providing genuine confidentiality and
    tamper detection.
    """

    hook_name = "auth-sha256-ctr"
    _IV_LEN = 16
    _TAG_LEN = 32

    def __init__(self, master_key: bytes):
        if not master_key or len(master_key) < 16:
            raise CryptoError("master_key must be at least 16 bytes")
        self._enc_key = hashlib.sha256(b"coredb:enc:" + master_key).digest()
        self._mac_key = hashlib.sha256(b"coredb:mac:" + master_key).digest()

    def _keystream(self, iv: bytes, length: int) -> bytes:
        chunks = []
        counter = 0
        produced = 0
        while produced < length:
            block = hashlib.sha256(
                self._enc_key + iv + counter.to_bytes(8, "big")
            ).digest()
            chunks.append(block)
            produced += len(block)
            counter += 1
        return b"".join(chunks)[:length]

    @staticmethod
    def _xor(a: bytes, b: bytes) -> bytes:
        return bytes(x ^ y for x, y in zip(a, b))

    def encrypt(self, plaintext: bytes, context: Optional[bytes] = None) -> bytes:
        iv = os.urandom(self._IV_LEN)
        ks = self._keystream(iv, len(plaintext))
        ct = self._xor(plaintext, ks)
        aad = context or b""
        tag = hmac.new(self._mac_key, aad + iv + ct, hashlib.sha256).digest()
        return iv + ct + tag

    def decrypt(self, ciphertext: bytes, context: Optional[bytes] = None) -> bytes:
        min_len = self._IV_LEN + self._TAG_LEN
        if len(ciphertext) < min_len:
            raise CryptoError("ciphertext truncated or malformed")
        iv = ciphertext[:self._IV_LEN]
        tag = ciphertext[-self._TAG_LEN:]
        ct = ciphertext[self._IV_LEN:-self._TAG_LEN]
        aad = context or b""
        expected = hmac.new(self._mac_key, aad + iv + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise CryptoError("decryption authentication failed (wrong key or tampered object)")
        ks = self._keystream(iv, len(ct))
        return self._xor(ct, ks)


class CallbackEncryptionHook(EncryptionHook):
    """Adapter allowing callers to inject custom encrypt/decrypt functions."""

    hook_name = "callback"

    def __init__(
        self,
        encrypt_fn: Callable[[bytes, Optional[bytes]], bytes],
        decrypt_fn: Callable[[bytes, Optional[bytes]], bytes],
    ):
        self._encrypt_fn = encrypt_fn
        self._decrypt_fn = decrypt_fn

    def encrypt(self, plaintext: bytes, context: Optional[bytes] = None) -> bytes:
        try:
            return self._encrypt_fn(plaintext, context)
        except CryptoError:
            raise
        except Exception as exc:
            raise CryptoError(f"encryption hook failed: {exc}") from exc

    def decrypt(self, ciphertext: bytes, context: Optional[bytes] = None) -> bytes:
        try:
            return self._decrypt_fn(ciphertext, context)
        except CryptoError:
            raise
        except Exception as exc:
            raise CryptoError(f"decryption hook failed: {exc}") from exc
