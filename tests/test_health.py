"""Tests for tools/health.py — health overview and deployment map."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from core.models import (
    ConnectorInfo,
    DelegateInfo,
    ExecutionStatus,
    FailureInfo,
    PipelineExecution,
    StageExecution,
)
from tools.health import register_health_tools

_NOW_MS = int(time.time() * 1000)
_HOUR_MS = 60 * 60 * 1000


def _make_delegate(name: str, connected: bool) -> DelegateInfo:
    return DelegateInfo(
        identifier=name,
        name=name,
        status="CONNECTED" if connected else "DISCONNECTED",
        actively_connected=connected,
        last_heartbeat=_NOW_MS - 60_000,
    )


def _make_connector(name: str, status: str, error: str = "") -> ConnectorInfo:
    return ConnectorInfo(
        identifier=name,
        name=name,
        connector_type="Github",
        status=status,
        last_tested=_NOW_MS - 3600_000,
        error_summary=error,
    )


def _make_execution(
    pipeline: str = "deploy_service",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    hours_ago: float = 1,
    run_sequence: int = 1,
    services: list[str] | None = None,
    envs: list[str] | None = None,
    failed_stage: str | None = None,
) -> PipelineExecution:
    stages = []
    if failed_stage:
        stages.append(
            StageExecution(
                stage_id="s1",
                name=failed_stage,
                identifier=failed_stage,
                status=ExecutionStatus.FAILED,
                failure_info=FailureInfo(message="boom"),
            )
        )
    return PipelineExecution(
        plan_execution_id=f"exec_{pipeline}_{run_sequence}",
        pipeline_identifier=pipeline,
        pipeline_name=pipeline,
        status=status,
        start_ts=int(_NOW_MS - hours_ago * _HOUR_MS),
        end_ts=int(_NOW_MS - hours_ago * _HOUR_MS + 600_000),
        run_sequence=run_sequence,
        stages=stages,
        service_identifiers=services or [],
        env_identifiers=envs or [],
    )


def _provider(
    delegates: list[DelegateInfo] | Exception | None = None,
    connectors: list[ConnectorInfo] | Exception | None = None,
    executions: list[PipelineExecution] | Exception | None = None,
) -> AsyncMock:
    provider = AsyncMock()
    for attr, value in [
        ("list_delegates", delegates or []),
        ("list_connectors", connectors or []),
        ("list_project_executions", executions or []),
    ]:
        if isinstance(value, Exception):
            setattr(provider, attr, AsyncMock(side_effect=value))
        else:
            setattr(provider, attr, AsyncMock(return_value=value))
    return provider


async def _call(tool_name: str, provider: AsyncMock) -> str:
    with patch("tools.health.get_provider", return_value=provider):
        mcp = FastMCP("test")
        register_health_tools(mcp)
        tool_fn = mcp._tool_manager._tools[tool_name].fn
        return await tool_fn()


class TestHealthOverview:
    @pytest.mark.asyncio
    async def test_all_clear(self) -> None:
        provider = _provider(
            delegates=[_make_delegate("d1", True)],
            connectors=[_make_connector("gh", "SUCCESS")],
            executions=[_make_execution(status=ExecutionStatus.SUCCESS)],
        )
        result = await _call("hawkeye_health_overview", provider)
        assert "All clear" in result
        assert "1/1 connected" in result
        assert "0 failing" in result
        assert "No failed executions" in result

    @pytest.mark.asyncio
    async def test_disconnected_delegate_flagged(self) -> None:
        provider = _provider(
            delegates=[_make_delegate("d1", True), _make_delegate("d2", False)],
        )
        result = await _call("hawkeye_health_overview", provider)
        assert "issue(s) need attention" in result
        assert "1 DISCONNECTED" in result
        assert "d2" in result

    @pytest.mark.asyncio
    async def test_failing_connector_flagged(self) -> None:
        provider = _provider(
            connectors=[
                _make_connector("gh", "SUCCESS"),
                _make_connector("docker", "FAILURE", error="401 unauthorized"),
            ],
        )
        result = await _call("hawkeye_health_overview", provider)
        assert "1 failing" in result
        assert "docker" in result
        assert "401 unauthorized" in result

    @pytest.mark.asyncio
    async def test_recent_failures_in_24h_window(self) -> None:
        provider = _provider(
            executions=[
                _make_execution(
                    "pipe_a", ExecutionStatus.FAILED, hours_ago=2,
                    run_sequence=7, failed_stage="Deploy",
                ),
                # older than 24h — must NOT appear
                _make_execution(
                    "pipe_old", ExecutionStatus.FAILED, hours_ago=30,
                    run_sequence=3, failed_stage="Build",
                ),
            ],
        )
        result = await _call("hawkeye_health_overview", provider)
        assert "1 failed execution" in result
        assert "pipe_a" in result
        assert "Deploy" in result
        assert "pipe_old" not in result

    @pytest.mark.asyncio
    async def test_pending_approvals_listed(self) -> None:
        provider = _provider(
            executions=[
                _make_execution(
                    "release_prod", ExecutionStatus.APPROVAL_WAITING, hours_ago=5,
                    run_sequence=12,
                ),
            ],
        )
        result = await _call("hawkeye_health_overview", provider)
        assert "Waiting on humans" in result
        assert "release_prod" in result
        assert "ApprovalWaiting" in result

    @pytest.mark.asyncio
    async def test_running_executions_listed(self) -> None:
        provider = _provider(
            executions=[
                _make_execution("live_deploy", ExecutionStatus.RUNNING, hours_ago=0.5),
            ],
        )
        result = await _call("hawkeye_health_overview", provider)
        assert "Currently running" in result
        assert "live_deploy" in result

    @pytest.mark.asyncio
    async def test_section_degrades_on_api_error(self) -> None:
        provider = _provider(
            delegates=RuntimeError("delegate API down"),
            connectors=[_make_connector("gh", "SUCCESS")],
            executions=[],
        )
        result = await _call("hawkeye_health_overview", provider)
        # Delegate section shows the error, other sections still render
        assert "Could not fetch delegates" in result
        assert "Connectors" in result
        assert "0 failing" in result


class TestDeploymentMap:
    @pytest.mark.asyncio
    async def test_matrix_from_cd_executions(self) -> None:
        provider = _provider(
            executions=[
                _make_execution(
                    "deploy_api", ExecutionStatus.SUCCESS, hours_ago=2,
                    run_sequence=42, services=["api"], envs=["prod"],
                ),
                _make_execution(
                    "deploy_api", ExecutionStatus.SUCCESS, hours_ago=50,
                    run_sequence=40, services=["api"], envs=["staging"],
                ),
                _make_execution(
                    "deploy_web", ExecutionStatus.SUCCESS, hours_ago=5,
                    run_sequence=9, services=["web"], envs=["prod"],
                ),
            ],
        )
        result = await _call("hawkeye_deployment_map", provider)
        assert "Deployment Map" in result
        assert "**api**" in result
        assert "**web**" in result
        assert "prod" in result
        assert "staging" in result
        assert "#42" in result
        # web was never deployed to staging → empty cell
        assert "—" in result

    @pytest.mark.asyncio
    async def test_latest_success_wins_per_cell(self) -> None:
        provider = _provider(
            executions=[
                _make_execution(
                    "deploy_api", ExecutionStatus.SUCCESS, hours_ago=10,
                    run_sequence=41, services=["api"], envs=["prod"],
                ),
                _make_execution(
                    "deploy_api", ExecutionStatus.SUCCESS, hours_ago=1,
                    run_sequence=43, services=["api"], envs=["prod"],
                ),
                # a newer FAILED run must not displace the last success
                _make_execution(
                    "deploy_api", ExecutionStatus.FAILED, hours_ago=0.2,
                    run_sequence=44, services=["api"], envs=["prod"],
                ),
            ],
        )
        result = await _call("hawkeye_deployment_map", provider)
        assert "#43" in result
        assert "#41" not in result
        assert "#44" not in result

    @pytest.mark.asyncio
    async def test_no_cd_executions(self) -> None:
        provider = _provider(
            executions=[_make_execution("ci_build", ExecutionStatus.SUCCESS)],
        )
        result = await _call("hawkeye_deployment_map", provider)
        assert "No CD deployments found" in result

    @pytest.mark.asyncio
    async def test_cd_seen_but_none_successful(self) -> None:
        provider = _provider(
            executions=[
                _make_execution(
                    "deploy_api", ExecutionStatus.FAILED, hours_ago=1,
                    services=["api"], envs=["prod"], failed_stage="Deploy",
                ),
            ],
        )
        result = await _call("hawkeye_deployment_map", provider)
        assert "none succeeded" in result
