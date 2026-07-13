"""Tests for the secrets toolset — parser, client endpoints, and the 2 tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models import HarnessProfile, SecretInfo
from providers.harness.client import HarnessAPIError, HarnessClient
from providers.harness.parser import parse_secret_item, parse_secret_list
from tools.secrets import (
    _format_secret_detail,
    _format_secret_table,
    _split_scope_prefix,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_secret_info(
    identifier: str = "global_git_hub_pat",
    name: str = "Global GitHub PAT",
    secret_type: str = "SecretText",
    secret_manager: str = "harnessSecretManager",
    value_type: str = "Inline",
    scope: str = "account",
    draft: bool = False,
) -> SecretInfo:
    return SecretInfo(
        identifier=identifier,
        name=name,
        secret_type=secret_type,
        secret_manager=secret_manager,
        value_type=value_type,
        scope=scope,
        description="Classic PAT for submodule access",
        tags={"team": "platform"},
        created=1_709_337_600_000,
        last_modified=1_709_337_600_000,
        draft=draft,
    )


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.list_secrets.return_value = []

    async def _get_provider():
        return provider

    with (
        patch("tools.secrets.get_provider", side_effect=_get_provider),
        patch("tools._helpers.get_provider", side_effect=_get_provider),
        patch("tools._helpers.log_tool_call"),
    ):
        yield provider


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
def client(profile: HarnessProfile) -> HarnessClient:
    with patch("providers.harness.client.get_token", return_value="tok_secret"):
        yield HarnessClient(profile)


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------


class TestParseSecretList:
    def test_parses_all_items(self, sample_secret_list_response: dict) -> None:
        items = sample_secret_list_response["data"]["content"]
        secrets = parse_secret_list(items)
        assert len(secrets) == 3
        assert secrets[0].identifier == "global_git_hub_pat"
        assert secrets[0].secret_type == "SecretText"
        assert secrets[0].secret_manager == "harnessSecretManager"
        assert secrets[0].value_type == "Inline"

    def test_scope_inference(self, sample_secret_list_response: dict) -> None:
        items = sample_secret_list_response["data"]["content"]
        secrets = parse_secret_list(items)
        # No org/project identifiers → account scope
        assert secrets[0].scope == "account"
        # Both identifiers → project scope
        assert secrets[1].scope == "project"
        # Org identifier only → org scope
        assert secrets[2].scope == "org"

    def test_timestamps_and_draft(self, sample_secret_list_response: dict) -> None:
        items = sample_secret_list_response["data"]["content"]
        secrets = parse_secret_list(items)
        assert secrets[0].created == 1709337600000
        assert secrets[1].last_modified == 1751500000000
        assert secrets[2].draft is True

    def test_model_never_carries_a_value(self, sample_secret_list_response: dict) -> None:
        """SecretInfo has no field that could hold the secret value."""
        items = sample_secret_list_response["data"]["content"]
        secret = parse_secret_list(items)[0]
        assert "value" not in secret.model_dump()

    def test_single_item_parse(self) -> None:
        item = {
            "secret": {
                "type": "SecretFile",
                "name": "CA Bundle",
                "identifier": "ca_bundle",
                "spec": {"secretManagerIdentifier": "vault"},
            },
            "createdAt": 100,
            "updatedAt": 200,
        }
        secret = parse_secret_item(item)
        assert secret.secret_type == "SecretFile"
        assert secret.secret_manager == "vault"
        assert secret.created == 100
        assert secret.last_modified == 200

    def test_non_dict_items_skipped(self) -> None:
        assert parse_secret_list(["bogus", None, 42]) == []


# ------------------------------------------------------------------
# Client endpoints
# ------------------------------------------------------------------


class TestSecretEndpoints:
    @respx.mock
    @pytest.mark.asyncio
    async def test_list_secrets_project_scope(
        self, client: HarnessClient, sample_secret_list_response: dict
    ) -> None:
        route = respx.get("https://app.harness.io/ng/api/v2/secrets").mock(
            return_value=httpx.Response(200, json=sample_secret_list_response)
        )
        secrets = await client.list_secrets()
        assert route.called
        params = dict(httpx.QueryParams(route.calls[0].request.url.query))
        assert params["accountIdentifier"] == "acc123"
        assert params["orgIdentifier"] == "default"
        assert params["projectIdentifier"] == "hawkeye_proj"
        assert len(secrets) == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_secrets_account_scope_omits_org_project(
        self, client: HarnessClient, sample_secret_list_response: dict
    ) -> None:
        route = respx.get("https://app.harness.io/ng/api/v2/secrets").mock(
            return_value=httpx.Response(200, json=sample_secret_list_response)
        )
        await client.list_secrets(scope="account")
        params = dict(httpx.QueryParams(route.calls[0].request.url.query))
        assert params["accountIdentifier"] == "acc123"
        assert "orgIdentifier" not in params
        assert "projectIdentifier" not in params

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_secrets_search_term(
        self, client: HarnessClient, sample_secret_list_response: dict
    ) -> None:
        route = respx.get("https://app.harness.io/ng/api/v2/secrets").mock(
            return_value=httpx.Response(200, json=sample_secret_list_response)
        )
        await client.list_secrets(search="github")
        params = dict(httpx.QueryParams(route.calls[0].request.url.query))
        assert params["searchTerm"] == "github"

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_secret_account_prefix_overrides_scope(
        self, client: HarnessClient, sample_secret_list_response: dict
    ) -> None:
        single = {
            "status": "SUCCESS",
            "data": sample_secret_list_response["data"]["content"][0],
        }
        route = respx.get(
            "https://app.harness.io/ng/api/v2/secrets/global_git_hub_pat"
        ).mock(return_value=httpx.Response(200, json=single))
        secret = await client.get_secret("account.global_git_hub_pat")
        assert route.called
        params = dict(httpx.QueryParams(route.calls[0].request.url.query))
        assert "orgIdentifier" not in params
        assert "projectIdentifier" not in params
        assert secret.identifier == "global_git_hub_pat"
        assert secret.scope == "account"

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_secret_404_raises(self, client: HarnessClient) -> None:
        respx.get("https://app.harness.io/ng/api/v2/secrets/nope").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        with pytest.raises(HarnessAPIError) as exc_info:
            await client.get_secret("nope")
        assert exc_info.value.status_code == 404


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------


class TestFormatSecretTable:
    def test_basic_table(self) -> None:
        result = _format_secret_table([_make_secret_info()], "account")
        assert "Secrets (1 found, account scope)" in result
        assert "Global GitHub PAT" in result
        assert "`global_git_hub_pat`" in result
        assert "harnessSecretManager" in result
        assert "values are never accessible" in result

    def test_missing_fields_show_dash(self) -> None:
        result = _format_secret_table(
            [_make_secret_info(secret_type="", secret_manager="")], "project"
        )
        assert "—" in result


class TestFormatSecretDetail:
    def test_metadata_lines(self) -> None:
        result = _format_secret_detail(_make_secret_info(), "account")
        assert "Secret: Global GitHub PAT (`global_git_hub_pat`)" in result
        assert "**Type:** SecretText (Inline)" in result
        assert "**Scope:** account" in result
        assert "**Secret Manager:** harnessSecretManager" in result
        assert "**Created:** 2024-03-02" in result
        assert "**Last modified:** 2024-03-02" in result
        assert "Classic PAT for submodule access" in result
        assert "team:platform" in result

    def test_rotation_note_present(self) -> None:
        result = _format_secret_detail(_make_secret_info(), "account")
        assert "last rotated" in result
        assert "not" in result and "visible" in result

    def test_draft_flag(self) -> None:
        result = _format_secret_detail(_make_secret_info(draft=True), "org")
        assert "Draft" in result


class TestSplitScopePrefix:
    def test_account_prefix(self) -> None:
        assert _split_scope_prefix("account.pat") == ("account", "pat")

    def test_org_prefix(self) -> None:
        assert _split_scope_prefix("org.pat") == ("org", "pat")

    def test_no_prefix(self) -> None:
        assert _split_scope_prefix("pat") == ("project", "pat")


# ------------------------------------------------------------------
# Tool integration tests
# ------------------------------------------------------------------


class TestHawkeyeListSecrets:
    @pytest.mark.asyncio
    async def test_output_format(self, mock_provider) -> None:
        mock_provider.list_secrets.return_value = [_make_secret_info()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        result = await tool_fn()
        assert "Global GitHub PAT" in result

    @pytest.mark.asyncio
    async def test_search_passed_to_provider(self, mock_provider) -> None:
        mock_provider.list_secrets.return_value = [_make_secret_info()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        await tool_fn(search="github")
        mock_provider.list_secrets.assert_awaited_once_with(
            search="github", scope="project"
        )

    @pytest.mark.asyncio
    async def test_type_filter_applied_client_side(self, mock_provider) -> None:
        mock_provider.list_secrets.return_value = [
            _make_secret_info(identifier="pat", secret_type="SecretText"),
            _make_secret_info(identifier="key", name="SSH", secret_type="SSHKey"),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        result = await tool_fn(secret_type="sshkey")
        assert "`key`" in result
        assert "`pat`" not in result

    @pytest.mark.asyncio
    async def test_invalid_secret_type_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        result = await tool_fn(secret_type="bogus")
        assert "Unknown secret type" in result
        mock_provider.list_secrets.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_scope_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        result = await tool_fn(scope="galaxy")
        assert "Unknown scope" in result
        mock_provider.list_secrets.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_secrets_found(self, mock_provider) -> None:
        mock_provider.list_secrets.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        result = await tool_fn(search="nothing")
        assert "No secrets found" in result

    @pytest.mark.asyncio
    async def test_403_returns_permission_hint(self, mock_provider) -> None:
        mock_provider.list_secrets.side_effect = HarnessAPIError(403, "forbidden")
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_secrets"].fn
        result = await tool_fn()
        assert "core_secret_view" in result


class TestHawkeyeCheckSecret:
    @pytest.mark.asyncio
    async def test_exact_identifier(self, mock_provider) -> None:
        mock_provider.get_secret.return_value = _make_secret_info()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="global_git_hub_pat")
        assert "Global GitHub PAT" in result
        mock_provider.get_secret.assert_awaited_once_with(
            "global_git_hub_pat", scope="project"
        )
        mock_provider.list_secrets.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_prefix_overrides_scope(self, mock_provider) -> None:
        mock_provider.get_secret.return_value = _make_secret_info()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="account.global_git_hub_pat")
        assert "**Scope:** account" in result
        mock_provider.get_secret.assert_awaited_once_with(
            "global_git_hub_pat", scope="account"
        )

    @pytest.mark.asyncio
    async def test_fuzzy_fallback_after_404(self, mock_provider) -> None:
        mock_provider.get_secret.side_effect = [
            HarnessAPIError(404, "not found"),
            _make_secret_info(),
        ]
        mock_provider.list_secrets.return_value = [_make_secret_info()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="github pat")
        assert "Global GitHub PAT" in result
        assert mock_provider.get_secret.await_count == 2
        # Second call uses the fuzzy-resolved identifier
        assert mock_provider.get_secret.await_args_list[1].args == (
            "global_git_hub_pat",
        )

    @pytest.mark.asyncio
    async def test_not_found_with_no_similar(self, mock_provider) -> None:
        mock_provider.get_secret.side_effect = HarnessAPIError(404, "not found")
        mock_provider.list_secrets.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="zzzz")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_empty_ref_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="  ")
        assert "Provide a secret identifier" in result
        mock_provider.get_secret.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_403_returns_permission_hint(self, mock_provider) -> None:
        mock_provider.get_secret.side_effect = HarnessAPIError(403, "forbidden")
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="global_git_hub_pat")
        assert "core_secret_view" in result

    @pytest.mark.asyncio
    async def test_invalid_scope_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_secret"].fn
        result = await tool_fn(secret_ref="pat", scope="galaxy")
        assert "Unknown scope" in result
        mock_provider.get_secret.assert_not_awaited()
