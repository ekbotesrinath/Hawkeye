"""Tests for tools/connectors.py and connector-related HarnessClient methods."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models import ConnectorInfo, HarnessProfile

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

_NOW_MS = int(time.time() * 1000)


def _make_profile() -> HarnessProfile:
    return HarnessProfile(
        name="test",
        account_id="acc123",
        org_identifier="default",
        project_identifier="proj",
        base_url="https://app.harness.io",
        is_active=True,
    )


def _make_connector(
    identifier: str = "GHConnector",
    name: str = "GitHub Connector",
    connector_type: str = "Git",
    status: str = "SUCCESS",
    last_tested: int | None = None,
    scope: str = "project",
    delegate_used: str = "",
    error_summary: str = "",
) -> ConnectorInfo:
    return ConnectorInfo(
        identifier=identifier,
        name=name,
        connector_type=connector_type,
        status=status,
        last_tested=last_tested if last_tested is not None else _NOW_MS - 60_000,
        scope=scope,
        delegate_used=delegate_used,
        error_summary=error_summary,
    )


def _mock_provider(
    connectors: list[ConnectorInfo] | None = None,
    test_result: dict | None = None,
) -> AsyncMock:
    provider = AsyncMock()
    provider.list_connectors = AsyncMock(return_value=connectors or [])
    provider.test_connector = AsyncMock(return_value=test_result or {"status": "SUCCESS"})
    return provider


# ------------------------------------------------------------------
# hawkeye_list_connectors
# ------------------------------------------------------------------


class TestListConnectors:
    @pytest.mark.asyncio
    async def test_list_connectors_formats_table(self) -> None:
        connectors = [
            _make_connector(
                identifier="GHConnector",
                name="GitHub Connector",
                connector_type="Git",
                status="SUCCESS",
                scope="account",
            ),
            _make_connector(
                identifier="ACRConnector",
                name="ACR Registry",
                connector_type="DockerRegistry",
                status="FAILURE",
                scope="project",
                error_summary="Auth failed",
            ),
            _make_connector(
                identifier="AzureConn",
                name="Azure Cloud",
                connector_type="Azure",
                status="UNKNOWN",
                scope="org",
                last_tested=None,
            ),
        ]
        provider = _mock_provider(connectors=connectors)

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_list_connectors"].fn
            result = await tool_fn(search="")

        # Table headers
        assert "| Name |" in result
        assert "| Type |" in result or "Type" in result
        # Status icons
        assert "✅" in result  # SUCCESS
        assert "❌" in result  # FAILURE
        assert "❓" in result  # UNKNOWN
        # Connector names
        assert "GitHub Connector" in result
        assert "ACR Registry" in result
        assert "Azure Cloud" in result
        # Summary
        assert "1/3" in result

    @pytest.mark.asyncio
    async def test_list_connectors_with_search(self) -> None:
        connectors = [
            _make_connector(identifier="GHConnector", name="GitHub Connector"),
        ]
        provider = _mock_provider(connectors=connectors)

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_list_connectors"].fn
            await tool_fn(search="GitHub")

        # Verify search was passed through
        provider.list_connectors.assert_awaited_once_with(search="GitHub")

    @pytest.mark.asyncio
    async def test_list_connectors_empty(self) -> None:
        provider = _mock_provider(connectors=[])

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_list_connectors"].fn
            result = await tool_fn(search="")

        assert "No connectors found" in result


# ------------------------------------------------------------------
# hawkeye_check_connector
# ------------------------------------------------------------------


class TestCheckConnector:
    @pytest.mark.asyncio
    async def test_check_connector_success(self) -> None:
        test_result = {
            "status": "SUCCESS",
            "testedAt": _NOW_MS - 30_000,
            "delegateId": "newad-eswd-tanr-dlgt",
            "errorSummary": "",
            "errors": [],
        }
        provider = _mock_provider(test_result=test_result)

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_connector"].fn
            result = await tool_fn(identifier="GHConnector")

        assert "✅" in result
        assert "SUCCESS" in result
        assert "GHConnector" in result
        assert "newad-eswd-tanr-dlgt" in result
        assert "project" in result
        # Should NOT contain error details section
        assert "Error Details" not in result

    @pytest.mark.asyncio
    async def test_check_connector_failure(self) -> None:
        test_result = {
            "status": "FAILURE",
            "testedAt": _NOW_MS - 60_000,
            "delegateId": "newad-eswd-tanr-dlgt",
            "errorSummary": "Connection refused: host unreachable",
            "errors": [
                {"message": "TCP connection failed to github.com:443"},
                {"message": "Delegate timeout after 30s"},
            ],
        }
        provider = _mock_provider(test_result=test_result)

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_connector"].fn
            result = await tool_fn(identifier="GHConnector")

        assert "❌" in result
        assert "FAILURE" in result
        assert "Error Details" in result
        assert "Connection refused" in result
        assert "TCP connection failed" in result
        assert "Delegate timeout" in result

    @pytest.mark.asyncio
    async def test_check_connector_account_scoped(self) -> None:
        test_result = {
            "status": "SUCCESS",
            "testedAt": _NOW_MS,
            "delegateId": "delegate1",
        }
        provider = _mock_provider(test_result=test_result)

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_connector"].fn
            result = await tool_fn(identifier="account.GHConnector")

        # Should parse scope correctly
        assert "account" in result
        assert "GHConnector" in result
        # Provider should have been called with the full prefixed identifier
        provider.test_connector.assert_awaited_once_with("account.GHConnector")

    @pytest.mark.asyncio
    async def test_check_connector_project_scoped(self) -> None:
        test_result = {
            "status": "SUCCESS",
            "testedAt": _NOW_MS,
            "delegateId": "delegate1",
        }
        provider = _mock_provider(test_result=test_result)

        with patch("tools.connectors.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.connectors import register_connector_tools

            mcp = FastMCP("test")
            register_connector_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_connector"].fn
            result = await tool_fn(identifier="GHConnector")

        # No prefix = project scope
        assert "project" in result
        provider.test_connector.assert_awaited_once_with("GHConnector")


# ------------------------------------------------------------------
# HarnessClient connector methods
# ------------------------------------------------------------------


class TestHarnessClientListConnectors:
    @respx.mock
    @pytest.mark.asyncio
    async def test_list_connectors_parses_response(self) -> None:
        api_response = {
            "status": "SUCCESS",
            "data": {
                "content": [
                    {
                        "connector": {
                            "identifier": "GHConnector",
                            "name": "GitHub Connector",
                            "type": "Git",
                            "orgIdentifier": "default",
                            "projectIdentifier": "proj",
                        },
                        "status": {
                            "status": "SUCCESS",
                            "testedAt": _NOW_MS - 120_000,
                            "delegateId": "delegate1",
                            "errorSummary": None,
                        },
                    },
                    {
                        "connector": {
                            "identifier": "ACRConn",
                            "name": "ACR Registry",
                            "type": "DockerRegistry",
                            "orgIdentifier": "default",
                            "projectIdentifier": "proj",
                        },
                        "status": {
                            "status": "FAILURE",
                            "testedAt": _NOW_MS - 60_000,
                            "delegateId": "delegate2",
                            "errorSummary": "Auth error",
                        },
                    },
                    {
                        "connector": {
                            "identifier": "AccountConn",
                            "name": "Account Level",
                            "type": "Azure",
                        },
                        "status": {
                            "status": "SUCCESS",
                            "testedAt": _NOW_MS,
                        },
                    },
                ],
            },
        }
        respx.get("https://app.harness.io/ng/api/connectors").mock(
            return_value=httpx.Response(200, json=api_response)
        )

        with patch("providers.harness.client.get_token", return_value="tok_secret"):
            from providers.harness.client import HarnessClient

            client = HarnessClient(_make_profile())
            connectors = await client.list_connectors()

        assert len(connectors) == 3

        gh = next(c for c in connectors if c.identifier == "GHConnector")
        assert gh.name == "GitHub Connector"
        assert gh.connector_type == "Git"
        assert gh.status == "SUCCESS"
        assert gh.scope == "project"

        acr = next(c for c in connectors if c.identifier == "ACRConn")
        assert acr.status == "FAILURE"
        assert acr.error_summary == "Auth error"

        account = next(c for c in connectors if c.identifier == "AccountConn")
        assert account.scope == "account"


class TestHarnessClientTestConnector:
    @respx.mock
    @pytest.mark.asyncio
    async def test_test_connector_account_scope(self) -> None:
        """account.GHConnector should strip prefix and omit org/project params."""
        api_response = {
            "status": "SUCCESS",
            "data": {
                "status": "SUCCESS",
                "testedAt": _NOW_MS,
                "delegateId": "delegate1",
            },
        }
        route = respx.post(
            "https://app.harness.io/ng/api/connectors/testConnection/GHConnector"
        ).mock(return_value=httpx.Response(200, json=api_response))

        with patch("providers.harness.client.get_token", return_value="tok_secret"):
            from providers.harness.client import HarnessClient

            client = HarnessClient(_make_profile())
            result = await client.test_connector("account.GHConnector")

        assert result["status"] == "SUCCESS"
        assert route.called
        request = route.calls[0].request
        url_params = dict(request.url.params)
        assert "accountIdentifier" in url_params
        assert "orgIdentifier" not in url_params
        assert "projectIdentifier" not in url_params

    @respx.mock
    @pytest.mark.asyncio
    async def test_test_connector_project_scope(self) -> None:
        """Plain identifier should include org and project params."""
        api_response = {
            "status": "SUCCESS",
            "data": {
                "status": "SUCCESS",
                "testedAt": _NOW_MS,
                "delegateId": "delegate1",
            },
        }
        route = respx.post(
            "https://app.harness.io/ng/api/connectors/testConnection/MyConnector"
        ).mock(return_value=httpx.Response(200, json=api_response))

        with patch("providers.harness.client.get_token", return_value="tok_secret"):
            from providers.harness.client import HarnessClient

            client = HarnessClient(_make_profile())
            result = await client.test_connector("MyConnector")

        assert result["status"] == "SUCCESS"
        assert route.called
        request = route.calls[0].request
        url_params = dict(request.url.params)
        assert url_params["accountIdentifier"] == "acc123"
        assert url_params["orgIdentifier"] == "default"
        assert url_params["projectIdentifier"] == "proj"

    @pytest.mark.asyncio
    async def test_test_connector_is_not_blocked(self) -> None:
        """POST to testConnection must NOT be blocked by _BLOCKED_METHODS."""
        from providers.harness.client import _BLOCKED_METHODS

        # POST is not in _BLOCKED_METHODS (only PUT, DELETE, PATCH are)
        assert "POST" not in _BLOCKED_METHODS
