"""Key management port: wraps per-row data keys (envelope encryption, spec section 5 and 10)."""

from typing import Protocol


class KeyManager(Protocol):
    @property
    def active_key_id(self) -> str: ...

    def wrap(self, data_key: bytes, aad: bytes) -> tuple[str, bytes]:
        """Encrypt a data key under the active master key; returns (key id, wrapped bytes)."""
        ...

    def unwrap(self, key_id: str, wrapped: bytes, aad: bytes) -> bytes: ...
