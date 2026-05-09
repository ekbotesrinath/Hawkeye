"""Tests for config.settings — multi-tenant profile management."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import (
    create_profile,
    delete_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    set_active_profile,
)
from core.models import HarnessProfile


@pytest.fixture(autouse=True)
def _tmp_hawkeye_dir(tmp_path: Path):
    """Redirect ~/.hawkeye to a temp directory for every test."""
    hawkeye_dir = tmp_path / ".hawkeye"
    profiles_file = hawkeye_dir / "profiles.json"
    active_file = hawkeye_dir / "active_profile"
    log_dir = hawkeye_dir / "logs"

    with (
        patch("config.settings._HAWKEYE_DIR", hawkeye_dir),
        patch("config.settings._PROFILES_FILE", profiles_file),
        patch("config.settings._ACTIVE_FILE", active_file),
        patch("config.settings._LOG_DIR", log_dir),
    ):
        yield hawkeye_dir


class TestCreateProfile:
    def test_create_new_profile(self) -> None:
        p = create_profile("prod", "acc1", "org1", "proj1")
        assert isinstance(p, HarnessProfile)
        assert p.name == "prod"
        assert p.account_id == "acc1"

    def test_create_sets_default_base_url(self) -> None:
        p = create_profile("prod", "acc1", "org1", "proj1")
        assert p.base_url == "https://app.harness.io"

    def test_create_with_custom_base_url(self) -> None:
        p = create_profile("prod", "acc1", "org1", "proj1", base_url="https://harness.corp.com")
        assert p.base_url == "https://harness.corp.com"

    def test_create_duplicate_raises(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        with pytest.raises(ValueError, match="already exists"):
            create_profile("prod", "acc2", "org2", "proj2")

    def test_create_persists_to_disk(self, _tmp_hawkeye_dir: Path) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        profiles_file = _tmp_hawkeye_dir / "profiles.json"
        assert profiles_file.exists()
        data = json.loads(profiles_file.read_text(encoding="utf-8"))
        assert "prod" in data

    def test_create_multiple_profiles(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        create_profile("staging", "acc2", "org2", "proj2")
        profiles = list_profiles()
        assert len(profiles) == 2

    def test_create_auto_creates_directory(self, _tmp_hawkeye_dir: Path) -> None:
        assert not _tmp_hawkeye_dir.exists()
        create_profile("prod", "acc1", "org1", "proj1")
        assert _tmp_hawkeye_dir.exists()


class TestGetProfile:
    def test_get_existing(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        p = get_profile("prod")
        assert p is not None
        assert p.account_id == "acc1"

    def test_get_nonexistent_returns_none(self) -> None:
        assert get_profile("nonexistent") is None

    def test_get_preserves_all_fields(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1", base_url="https://custom.io")
        p = get_profile("prod")
        assert p is not None
        assert p.org_identifier == "org1"
        assert p.project_identifier == "proj1"
        assert p.base_url == "https://custom.io"


class TestListProfiles:
    def test_empty_list(self) -> None:
        profiles = list_profiles()
        assert profiles == []

    def test_list_after_create(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        create_profile("staging", "acc2", "org2", "proj2")
        profiles = list_profiles()
        names = {p.name for p in profiles}
        assert names == {"prod", "staging"}

    def test_list_shows_active_flag(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        set_active_profile("prod")
        profiles = list_profiles()
        assert profiles[0].is_active is True


class TestSetActiveProfile:
    def test_set_active(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        set_active_profile("prod")
        active = get_active_profile()
        assert active is not None
        assert active.name == "prod"

    def test_set_active_nonexistent_raises(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            set_active_profile("nonexistent")

    def test_switch_active_profile(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        create_profile("staging", "acc2", "org2", "proj2")
        set_active_profile("prod")
        assert get_active_profile().name == "prod"  # type: ignore[union-attr]
        set_active_profile("staging")
        assert get_active_profile().name == "staging"  # type: ignore[union-attr]


class TestGetActiveProfile:
    def test_no_active_returns_none(self) -> None:
        assert get_active_profile() is None

    def test_active_after_set(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        set_active_profile("prod")
        p = get_active_profile()
        assert p is not None
        assert p.name == "prod"


class TestDeleteProfile:
    def test_delete_existing(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        delete_profile("prod")
        assert get_profile("prod") is None

    def test_delete_nonexistent_raises(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            delete_profile("nonexistent")

    def test_delete_active_clears_active(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        set_active_profile("prod")
        delete_profile("prod")
        assert get_active_profile() is None

    def test_delete_non_active_preserves_active(self) -> None:
        create_profile("prod", "acc1", "org1", "proj1")
        create_profile("staging", "acc2", "org2", "proj2")
        set_active_profile("prod")
        delete_profile("staging")
        assert get_active_profile().name == "prod"  # type: ignore[union-attr]


class TestProfileValidation:
    def test_profile_required_fields(self) -> None:
        with pytest.raises((TypeError, Exception)):
            HarnessProfile()  # type: ignore[call-arg]

    def test_profile_model_dump_round_trip(self) -> None:
        p = HarnessProfile(
            name="test", account_id="a", org_identifier="o",
            project_identifier="p",
        )
        data = p.model_dump()
        p2 = HarnessProfile(**data)
        assert p.name == p2.name
        assert p.account_id == p2.account_id
