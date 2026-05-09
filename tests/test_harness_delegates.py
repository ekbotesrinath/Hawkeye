"""Tests for HarnessClient.list_delegates — delegate API parsing."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from core.models import HarnessProfile
from providers.harness.client import HarnessClient


@pytest.fixture()
def profile() -> HarnessProfile:
    return HarnessProfile(
        name="test-profile",
        account_id="acc123",
        org_identifier="default",
        project_identifier="hawkeye_proj",
        base_url="https://app.harness.io",
        is_active=True,
    )


@pytest.fixture()
def mock_token():
    with patch("providers.harness.client.get_token", return_value="tok_secret") as m:
        yield m


@pytest.fixture()
def client(profile: HarnessProfile, mock_token) -> HarnessClient:
    return HarnessClient(profile)


# ------------------------------------------------------------------
# list_delegates — response parsing
# ------------------------------------------------------------------


class TestListDelegatesParses:
    @respx.mock
    @pytest.mark.asyncio
    async def test_list_delegates_parses_response(self, client: HarnessClient) -> None:
        """Mock realistic Harness delegate JSON and verify DelegateInfo construction."""
        api_response = {
            "status": "SUCCESS",
            "data": {
                "resource": [
                    {
                        "delegateGroupIdentifier": "newad_tanr_dlgt",
                        "groupName": "newad-eswd-tanr-dlgt",
                        "activelyConnected": True,
                        "lastHeartBeat": 1715200000000,
                        "delegateType": "KUBERNETES",
                        "delegateInstanceDetails": [
                            {"status": "CONNECTED"},
                            {"status": "CONNECTED"},
                            {"status": "CONNECTED"},
                        ],
                        "groupImplicitSelectors": {
                            "profileName": ["newad-eswd-tanr-dlgt"],
                            "ng": ["true"],
                        },
                    },
                    {
                        "delegateGroupIdentifier": "newad_tngo_dlgt",
                        "groupName": "newad-eswd-tngo-dlgt",
                        "activelyConnected": False,
                        "lastHeartBeat": 1715100000000,
                        "delegateType": "KUBERNETES",
                        "delegateInstanceDetails": [],
                        "groupImplicitSelectors": {
                            "profileName": ["newad-eswd-tngo-dlgt"],
                        },
                    },
                ]
            },
        }
        respx.post(
            "https://app.harness.io/ng/api/delegate-setup/listDelegates"
        ).mock(return_value=httpx.Response(200, json=api_response))

        delegates = await client.list_delegates()

        assert len(delegates) == 2

        # First delegate
        d1 = delegates[0]
        assert d1.identifier == "newad_tanr_dlgt"
        assert d1.name == "newad-eswd-tanr-dlgt"
        assert d1.status == "CONNECTED"
        assert d1.actively_connected is True
        assert d1.last_heartbeat == 1715200000000
        assert d1.delegate_type == "KUBERNETES"
        assert d1.instances_connected == 3
        assert "newad-eswd-tanr-dlgt" in d1.tags
        assert "true" in d1.tags

        # Second delegate
        d2 = delegates[1]
        assert d2.identifier == "newad_tngo_dlgt"
        assert d2.name == "newad-eswd-tngo-dlgt"
        assert d2.status == "UNKNOWN"
        assert d2.actively_connected is False
        assert d2.instances_connected == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_delegates_handles_empty_response(self, client: HarnessClient) -> None:
        """Empty resource array returns empty list."""
        api_response = {
            "status": "SUCCESS",
            "data": {"resource": []},
        }
        respx.post(
            "https://app.harness.io/ng/api/delegate-setup/listDelegates"
        ).mock(return_value=httpx.Response(200, json=api_response))

        delegates = await client.list_delegates()
        assert delegates == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_delegates_handles_tags(self, client: HarnessClient) -> None:
        """Verify groupImplicitSelectors are extracted into tags."""
        api_response = {
            "status": "SUCCESS",
            "data": {
                "resource": [
                    {
                        "delegateGroupIdentifier": "dlgt1",
                        "groupName": "test-delegate",
                        "activelyConnected": True,
                        "lastHeartBeat": 1715200000000,
                        "delegateType": "DOCKER",
                        "delegateInstanceDetails": [
                            {"status": "CONNECTED"},
                        ],
                        "groupImplicitSelectors": {
                            "profileName": ["test-delegate"],
                            "ng": ["true"],
                            "custom": ["tag1", "tag2"],
                        },
                    }
                ]
            },
        }
        respx.post(
            "https://app.harness.io/ng/api/delegate-setup/listDelegates"
        ).mock(return_value=httpx.Response(200, json=api_response))

        delegates = await client.list_delegates()
        assert len(delegates) == 1
        d = delegates[0]
        assert "test-delegate" in d.tags
        assert "true" in d.tags
        assert "tag1" in d.tags
        assert "tag2" in d.tags
        assert d.delegate_type == "DOCKER"

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_delegates_handles_list_selectors(self, client: HarnessClient) -> None:
        """Verify list-type groupImplicitSelectors are handled."""
        api_response = {
            "status": "SUCCESS",
            "data": {
                "resource": [
                    {
                        "delegateGroupIdentifier": "dlgt1",
                        "groupName": "test-delegate",
                        "groupImplicitSelectors": ["sel1", "sel2"],
                    }
                ]
            },
        }
        respx.post(
            "https://app.harness.io/ng/api/delegate-setup/listDelegates"
        ).mock(return_value=httpx.Response(200, json=api_response))

        delegates = await client.list_delegates()
        assert len(delegates) == 1
        assert delegates[0].tags == ["sel1", "sel2"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_delegates_handles_missing_fields(self, client: HarnessClient) -> None:
        """Verify defaults when optional fields are missing from response."""
        api_response = {
            "status": "SUCCESS",
            "data": {
                "resource": [
                    {
                        "delegateGroupIdentifier": "dlgt1",
                        "groupName": "minimal-delegate",
                    }
                ]
            },
        }
        respx.post(
            "https://app.harness.io/ng/api/delegate-setup/listDelegates"
        ).mock(return_value=httpx.Response(200, json=api_response))

        delegates = await client.list_delegates()
        assert len(delegates) == 1
        d = delegates[0]
        assert d.identifier == "dlgt1"
        assert d.name == "minimal-delegate"
        assert d.status == "UNKNOWN"
        assert d.actively_connected is False
        assert d.last_heartbeat is None
        assert d.delegate_type == ""
        assert d.tags == []
        assert d.instances_connected == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_delegates_handles_direct_list_response(self, client: HarnessClient) -> None:
        """Handle response where data is a list directly (no resource wrapper)."""
        api_response = {
            "status": "SUCCESS",
            "data": [
                {
                    "delegateGroupIdentifier": "dlgt1",
                    "groupName": "direct-list-delegate",
                    "status": "CONNECTED",
                    "activelyConnected": True,
                }
            ],
        }
        respx.post(
            "https://app.harness.io/ng/api/delegate-setup/listDelegates"
        ).mock(return_value=httpx.Response(200, json=api_response))

        delegates = await client.list_delegates()
        assert len(delegates) == 1
        assert delegates[0].name == "direct-list-delegate"
