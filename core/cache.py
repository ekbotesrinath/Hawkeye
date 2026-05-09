"""Simple in-memory TTL cache for Hawkeye API responses.

Thread-safe LRU cache with configurable TTL per entry.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

DEFAULT_TTL = 60  # seconds
PIPELINE_DEF_TTL = 300
MAX_ENTRIES = 100


class TTLCache:
    """Thread-safe LRU cache with per-key TTL."""

    def __init__(self, max_size: int = MAX_ENTRIES) -> None:
        self._max_size = max_size
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """Retrieve a cached value, or ``None`` if missing / expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            # Move to end (most-recently used)
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
        """Store *value* under *key* with the given *ttl* in seconds."""
        with self._lock:
            expires_at = time.monotonic() + ttl
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expires_at)
            # Evict oldest if over capacity
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """Remove a single key from the cache."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
