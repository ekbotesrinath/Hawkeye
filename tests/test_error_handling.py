"""Tests for error handling — friendly error translation in run_tool."""

from __future__ import annotations

import httpx
import pytest

from providers.harness.client import HarnessAPIError
from tools._helpers import _friendly_error, run_tool

# ------------------------------------------------------------------
# _friendly_error unit tests
# ------------------------------------------------------------------


class TestFriendlyError:
    def test_401_expired_token(self) -> None:
        exc = HarnessAPIError(401, "Unauthorized")
        msg = _friendly_error(exc)
        assert "API token expired or invalid" in msg
        assert "make connect" in msg

    def test_403_insufficient_permissions(self) -> None:
        exc = HarnessAPIError(403, "Forbidden")
        msg = _friendly_error(exc)
        assert "Insufficient permissions" in msg

    def test_404_not_found(self) -> None:
        exc = HarnessAPIError(404, "Not Found")
        msg = _friendly_error(exc)
        assert "not found" in msg
        assert "Check the ID or URL" in msg

    def test_other_harness_api_error(self) -> None:
        exc = HarnessAPIError(500, "Internal Server Error")
        msg = _friendly_error(exc)
        assert "500" in msg

    def test_connect_error(self) -> None:
        exc = httpx.ConnectError("Connection refused")
        msg = _friendly_error(exc)
        assert "Cannot reach Harness" in msg

    def test_timeout_error(self) -> None:
        exc = httpx.ReadTimeout("Read timed out")
        msg = _friendly_error(exc)
        assert "Cannot reach Harness" in msg

    def test_connection_error(self) -> None:
        exc = ConnectionError("Connection reset")
        msg = _friendly_error(exc)
        assert "Cannot reach Harness" in msg

    def test_os_error_network(self) -> None:
        exc = OSError("Network unreachable")
        msg = _friendly_error(exc)
        assert "Cannot reach Harness" in msg

    def test_no_profile_runtime_error(self) -> None:
        exc = RuntimeError(
            "No active Hawkeye profile configured. "
            "Run 'make connect' to set up your Harness account."
        )
        msg = _friendly_error(exc)
        assert "No Hawkeye profile configured" in msg
        assert "make connect" in msg

    def test_generic_runtime_error(self) -> None:
        exc = RuntimeError("Something else went wrong")
        msg = _friendly_error(exc)
        assert "Something else went wrong" in msg

    def test_value_error_passthrough(self) -> None:
        exc = ValueError("bad value")
        msg = _friendly_error(exc)
        assert "bad value" in msg


# ------------------------------------------------------------------
# run_tool integration with error handling
# ------------------------------------------------------------------


class TestRunToolErrorHandling:
    @pytest.mark.asyncio()
    async def test_401_in_run_tool(self) -> None:
        async def _failing():
            raise HarnessAPIError(401, "Unauthorized")

        result = await run_tool("test_tool", {}, _failing())
        assert "API token expired or invalid" in result

    @pytest.mark.asyncio()
    async def test_403_in_run_tool(self) -> None:
        async def _failing():
            raise HarnessAPIError(403, "Forbidden")

        result = await run_tool("test_tool", {}, _failing())
        assert "Insufficient permissions" in result

    @pytest.mark.asyncio()
    async def test_404_in_run_tool(self) -> None:
        async def _failing():
            raise HarnessAPIError(404, "Not Found")

        result = await run_tool("test_tool", {}, _failing())
        assert "not found" in result

    @pytest.mark.asyncio()
    async def test_network_error_in_run_tool(self) -> None:
        async def _failing():
            raise httpx.ConnectError("Connection refused")

        result = await run_tool("test_tool", {}, _failing())
        assert "Cannot reach Harness" in result

    @pytest.mark.asyncio()
    async def test_no_profile_in_run_tool(self) -> None:
        async def _failing():
            raise RuntimeError("No active Hawkeye profile configured. Run 'make connect'")

        result = await run_tool("test_tool", {}, _failing())
        assert "No Hawkeye profile configured" in result

    @pytest.mark.asyncio()
    async def test_success_path_unchanged(self) -> None:
        async def _ok():
            return "All good"

        result = await run_tool("test_tool", {}, _ok())
        assert "All good" in result
