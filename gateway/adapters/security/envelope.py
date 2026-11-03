"""Envelope encryption with AES-256-GCM: a fresh data key per secret, wrapped by a master key.

The master key comes from the environment today (MASTER_KEY, base64url 32 bytes); a KMS adapter
can implement the same KeyManager port later. The associated data binds each ciphertext to its
org, provider and row, so a ciphertext copied to another row or org fails to decrypt.
"""

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway.adapters.security.tokens import b64url_decode

_NONCE = 12


class DecryptionError(Exception):
    """Ciphertext, wrapped key or associated data does not match: never return partial plaintext."""


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:_NONCE], blob[_NONCE:], aad)
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError("cannot decrypt: wrong key, tampered data or wrong context") from exc


def parse_master_keys(active: str, active_id: str, previous: str) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for item in filter(None, (p.strip() for p in previous.split(","))):
        kid, sep, encoded = item.partition(":")
        if not sep:
            raise ValueError("MASTER_PREVIOUS_KEYS entries must look like kid:base64url")
        keys[kid] = b64url_decode(encoded)
    keys[active_id] = b64url_decode(active)
    if any(len(k) != 32 for k in keys.values()):
        raise ValueError("master keys must be base64url-encoded 32-byte values")
    return keys


class LocalKeyManager:
    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError("active master key id is not among the configured keys")
        self._keys = dict(keys)
        self._active = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active

    def wrap(self, data_key: bytes, aad: bytes) -> tuple[str, bytes]:
        return self._active, _seal(self._keys[self._active], data_key, aad)

    def unwrap(self, key_id: str, wrapped: bytes, aad: bytes) -> bytes:
        key = self._keys.get(key_id)
        if key is None:
            raise DecryptionError(f"master key {key_id!r} is not configured")
        return _open(key, wrapped, aad)


@dataclass(frozen=True, slots=True)
class Sealed:
    ciphertext: bytes
    wrapped_key: bytes
    key_id: str


class Envelope:
    def __init__(self, keys: LocalKeyManager) -> None:
        self._keys = keys

    def seal(self, plaintext: str, aad: bytes) -> Sealed:
        data_key = AESGCM.generate_key(bit_length=256)
        key_id, wrapped = self._keys.wrap(data_key, aad)
        return Sealed(_seal(data_key, plaintext.encode(), aad), wrapped, key_id)

    def open(self, sealed: Sealed, aad: bytes) -> str:
        data_key = self._keys.unwrap(sealed.key_id, sealed.wrapped_key, aad)
        return _open(data_key, sealed.ciphertext, aad).decode()
