"""Shared pytest fixtures for Hawkeye tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.models import HarnessProfile

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def mock_profile() -> HarnessProfile:
    """A test HarnessProfile."""
    return HarnessProfile(
        name="test-profile",
        account_id="acc_test_123",
        org_identifier="default",
        project_identifier="hawkeye_project",
        base_url="https://app.harness.io",
        is_active=True,
    )


@pytest.fixture()
def sample_execution_response() -> dict:
    """Realistic Harness execution detail API response (success)."""
    return json.loads((FIXTURES_DIR / "execution_success.json").read_text(encoding="utf-8"))


@pytest.fixture()
def sample_execution_failed_response() -> dict:
    """Realistic Harness execution detail API response (failed)."""
    return json.loads((FIXTURES_DIR / "execution_failed.json").read_text(encoding="utf-8"))


@pytest.fixture()
def sample_execution_graph_response() -> dict:
    """Realistic Harness execution graph API response."""
    return json.loads((FIXTURES_DIR / "execution_graph.json").read_text(encoding="utf-8"))


@pytest.fixture()
def sample_pipeline_list_response() -> list:
    """Realistic Harness v1 pipeline listing response."""
    return json.loads((FIXTURES_DIR / "pipeline_list.json").read_text(encoding="utf-8"))


@pytest.fixture()
def sample_input_sets_response() -> list:
    """Realistic Harness input sets listing response."""
    return json.loads((FIXTURES_DIR / "input_sets.json").read_text(encoding="utf-8"))
