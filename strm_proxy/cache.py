from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


Key = TypeVar("Key")
Value = TypeVar("Value")


@dataclass(frozen=True, slots=True)
class _Entry(Generic[Value]):
    expires_at: float
    value: Value


class TTLCache(Generic[Key, Value]):
    """Small in-memory TTL cache for immutable resolver results."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[Key, _Entry[Value]] = {}

    def get(self, key: Key) -> Value | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: Key, value: Value) -> None:
        self._entries[key] = _Entry(
            expires_at=self._clock() + self.ttl_seconds,
            value=value,
        )

    def clear(self) -> None:
        self._entries.clear()
