"""Tests for core.credentials — keyring-based credential management."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from keyring.errors import KeyringError

from core.credentials import (
    SERVICE_NAME,
    delete_token,
    get_token,
    has_token,
    store_token,
)


@pytest.fixture(autouse=True)
def _mock_keyring():
    """Patch keyring so tests never touch the real keychain."""
    store: dict[str, str] = {}

    def _set(service: str, key: str, value: str) -> None:
        store[f"{service}:{key}"] = value

    def _get(service: str, key: str) -> str | None:
        return store.get(f"{service}:{key}")

    def _delete(service: str, key: str) -> None:
        full = f"{service}:{key}"
        if full not in store:
            raise KeyringError("not found")
        del store[full]

    with (
        patch("core.credentials.keyring.set_password", side_effect=_set),
        patch("core.credentials.keyring.get_password", side_effect=_get),
        patch("core.credentials.keyring.delete_password", side_effect=_delete),
    ):
        yield store


class TestStoreToken:
    def test_store_and_retrieve(self) -> None:
        store_token("prod", "tok_secret_123")
        assert get_token("prod") == "tok_secret_123"

    def test_store_overwrites_existing(self) -> None:
        store_token("prod", "old")
        store_token("prod", "new")
        assert get_token("prod") == "new"

    def test_store_multiple_profiles(self) -> None:
        store_token("prod", "tok_prod")
        store_token("staging", "tok_staging")
        assert get_token("prod") == "tok_prod"
        assert get_token("staging") == "tok_staging"


class TestGetToken:
    def test_returns_none_for_missing(self) -> None:
        assert get_token("nonexistent") is None

    def test_get_after_store(self) -> None:
        store_token("dev", "tok_dev")
        assert get_token("dev") == "tok_dev"

    def test_keyring_error_propagates(self) -> None:
        with (
            patch("core.credentials.keyring.get_password", side_effect=KeyringError("fail")),
            pytest.raises(KeyringError),
        ):
            get_token("prod")


class TestDeleteToken:
    def test_delete_existing(self) -> None:
        store_token("prod", "tok")
        delete_token("prod")
        assert get_token("prod") is None

    def test_delete_nonexistent_raises(self) -> None:
        with pytest.raises(KeyringError):
            delete_token("nonexistent")

    def test_keyring_error_propagates(self) -> None:
        with (
            patch("core.credentials.keyring.delete_password", side_effect=KeyringError("fail")),
            pytest.raises(KeyringError),
        ):
            delete_token("any")


class TestHasToken:
    def test_has_token_true(self) -> None:
        store_token("prod", "tok")
        assert has_token("prod") is True

    def test_has_token_false(self) -> None:
        assert has_token("nonexistent") is False

    def test_has_token_keyring_error_returns_false(self) -> None:
        with patch("core.credentials.keyring.get_password", side_effect=KeyringError("fail")):
            assert has_token("prod") is False


class TestServiceName:
    def test_service_name_constant(self) -> None:
        assert SERVICE_NAME == "hawkeye"
