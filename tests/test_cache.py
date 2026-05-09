"""Tests for core.cache — TTL response cache."""

from __future__ import annotations

import threading
from unittest.mock import patch

from core.cache import DEFAULT_TTL, MAX_ENTRIES, PIPELINE_DEF_TTL, TTLCache


class TestGetSet:
    def test_set_and_get(self) -> None:
        cache = TTLCache()
        cache.set("k1", {"data": 1})
        assert cache.get("k1") == {"data": 1}

    def test_get_missing_returns_none(self) -> None:
        cache = TTLCache()
        assert cache.get("nonexistent") is None

    def test_overwrite_existing(self) -> None:
        cache = TTLCache()
        cache.set("k1", "old")
        cache.set("k1", "new")
        assert cache.get("k1") == "new"

    def test_multiple_keys(self) -> None:
        cache = TTLCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        assert cache.get("a") == 1
        assert cache.get("b") == 2
        assert cache.get("c") == 3


class TestTTLExpiration:
    def test_expired_key_returns_none(self) -> None:
        cache = TTLCache()
        # Use a mock to simulate time passing
        with patch("core.cache.time.monotonic", return_value=1000.0):
            cache.set("k1", "value", ttl=10)
        # 11 seconds later
        with patch("core.cache.time.monotonic", return_value=1011.0):
            assert cache.get("k1") is None

    def test_not_yet_expired(self) -> None:
        cache = TTLCache()
        with patch("core.cache.time.monotonic", return_value=1000.0):
            cache.set("k1", "value", ttl=60)
        with patch("core.cache.time.monotonic", return_value=1030.0):
            assert cache.get("k1") == "value"

    def test_different_ttls(self) -> None:
        cache = TTLCache()
        with patch("core.cache.time.monotonic", return_value=1000.0):
            cache.set("short", "s", ttl=10)
            cache.set("long", "l", ttl=300)
        with patch("core.cache.time.monotonic", return_value=1015.0):
            assert cache.get("short") is None
            assert cache.get("long") == "l"

    def test_exact_expiry_boundary(self) -> None:
        cache = TTLCache()
        with patch("core.cache.time.monotonic", return_value=1000.0):
            cache.set("k1", "v", ttl=10)
        # Exactly at expiry time: 1000 + 10 = 1010, monotonic > 1010 → expired
        with patch("core.cache.time.monotonic", return_value=1010.1):
            assert cache.get("k1") is None


class TestInvalidateAndClear:
    def test_invalidate_existing(self) -> None:
        cache = TTLCache()
        cache.set("k1", "v")
        cache.invalidate("k1")
        assert cache.get("k1") is None

    def test_invalidate_nonexistent_no_error(self) -> None:
        cache = TTLCache()
        cache.invalidate("nope")  # Should not raise

    def test_clear_removes_all(self) -> None:
        cache = TTLCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None
        assert len(cache) == 0


class TestLRUEviction:
    def test_evict_oldest_at_capacity(self) -> None:
        cache = TTLCache(max_size=3)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("d", 4)  # a should be evicted
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("d") == 4

    def test_get_refreshes_lru_order(self) -> None:
        cache = TTLCache(max_size=3)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.get("a")    # refreshes "a"
        cache.set("d", 4)  # "b" should be evicted (oldest)
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("d") == 4

    def test_size_tracking(self) -> None:
        cache = TTLCache(max_size=5)
        for i in range(10):
            cache.set(f"k{i}", i)
        assert len(cache) == 5


class TestThreadSafety:
    def test_concurrent_access(self) -> None:
        cache = TTLCache(max_size=200)
        errors: list[str] = []

        def writer(start: int) -> None:
            try:
                for i in range(100):
                    cache.set(f"w{start}_{i}", i)
            except Exception as e:
                errors.append(str(e))

        def reader(start: int) -> None:
            try:
                for i in range(100):
                    cache.get(f"w{start}_{i}")
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=reader, args=(0,)),
            threading.Thread(target=reader, args=(1,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread errors: {errors}"


class TestConstants:
    def test_default_ttl(self) -> None:
        assert DEFAULT_TTL == 60

    def test_pipeline_def_ttl(self) -> None:
        assert PIPELINE_DEF_TTL == 300

    def test_max_entries(self) -> None:
        assert MAX_ENTRIES == 100
