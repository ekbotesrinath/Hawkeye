"""Tests for providers.harness.client — Harness REST API client."""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models import ExecutionStatus, HarnessProfile
from providers.harness.client import (
    _BLOCKED_METHODS,
    _VERSION,
    HarnessAPIError,
    HarnessClient,
    ReadOnlyViolationError,
    _extract_log_key,
    _extract_logs_from_zip,
)


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


def _make_logs_zip(entries: list[dict[str, str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        content = "\n".join(json.dumps(entry) for entry in entries)
        archive.writestr("logs/run.ndjson", content)
    return buffer.getvalue()


# ------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------


class TestAuthentication:
    @respx.mock
    @pytest.mark.asyncio
    async def test_api_key_header_sent(self, client: HarnessClient) -> None:
        route = respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines"
        ).mock(return_value=httpx.Response(200, json=[]))

        await client.list_pipelines()
        assert route.called
        request = route.calls[0].request
        assert request.headers["x-api-key"] == "tok_secret"

    @respx.mock
    @pytest.mark.asyncio
    async def test_user_agent_header(self, client: HarnessClient) -> None:
        route = respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines"
        ).mock(return_value=httpx.Response(200, json=[]))

        await client.list_pipelines()
        request = route.calls[0].request
        assert f"Hawkeye/{_VERSION}" in request.headers["user-agent"]

    @pytest.mark.asyncio
    async def test_no_token_raises(self, profile: HarnessProfile) -> None:
        with patch("providers.harness.client.get_token", return_value=None):
            c = HarnessClient(profile)
            with pytest.raises(RuntimeError, match="No API token"):
                await c.list_pipelines()


# ------------------------------------------------------------------
# Read-only enforcement
# ------------------------------------------------------------------


class TestReadOnlyEnforcement:
    @pytest.mark.asyncio
    async def test_put_blocked(self, client: HarnessClient) -> None:
        with pytest.raises(ReadOnlyViolationError):
            await client._request("PUT", "/some/path")

    @pytest.mark.asyncio
    async def test_delete_blocked(self, client: HarnessClient) -> None:
        with pytest.raises(ReadOnlyViolationError):
            await client._request("DELETE", "/some/path")

    @pytest.mark.asyncio
    async def test_patch_blocked(self, client: HarnessClient) -> None:
        with pytest.raises(ReadOnlyViolationError):
            await client._request("PATCH", "/some/path")

    @pytest.mark.asyncio
    async def test_get_allowed(self, client: HarnessClient) -> None:
        with respx.mock:
            respx.get("https://app.harness.io/test").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await client._request("GET", "/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_post_allowed(self, client: HarnessClient) -> None:
        with respx.mock:
            respx.post("https://app.harness.io/test").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await client._request("POST", "/test")
            assert result == {"ok": True}

    def test_blocked_methods_set(self) -> None:
        assert {"PUT", "DELETE", "PATCH"} == _BLOCKED_METHODS


# ------------------------------------------------------------------
# Retry logic
# ------------------------------------------------------------------


class TestRetryLogic:
    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_on_429(self, client: HarnessClient) -> None:
        route = respx.get("https://app.harness.io/test").mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        with patch("providers.harness.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client._request("GET", "/test")
        assert result == {"ok": True}
        assert route.call_count == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_on_500(self, client: HarnessClient) -> None:
        respx.get("https://app.harness.io/test").mock(
            side_effect=[
                httpx.Response(500, text="server error"),
                httpx.Response(200, json={"data": 1}),
            ]
        )
        with patch("providers.harness.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client._request("GET", "/test")
        assert result == {"data": 1}

    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self, client: HarnessClient) -> None:
        respx.get("https://app.harness.io/test").mock(
            return_value=httpx.Response(503, text="unavailable")
        )
        with (
            patch("providers.harness.client.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HarnessAPIError),
        ):
            await client._request("GET", "/test")

    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_on_timeout(self, client: HarnessClient) -> None:
        respx.get("https://app.harness.io/test").mock(
            side_effect=[
                httpx.ReadTimeout("timeout"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        with patch("providers.harness.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client._request("GET", "/test")
        assert result == {"ok": True}


# ------------------------------------------------------------------
# Non-retryable errors
# ------------------------------------------------------------------


class TestNonRetryableErrors:
    @respx.mock
    @pytest.mark.asyncio
    async def test_401_raises_immediately(self, client: HarnessClient) -> None:
        respx.get("https://app.harness.io/test").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        with pytest.raises(HarnessAPIError) as exc_info:
            await client._request("GET", "/test")
        assert exc_info.value.status_code == 401

    @respx.mock
    @pytest.mark.asyncio
    async def test_404_raises_immediately(self, client: HarnessClient) -> None:
        respx.get("https://app.harness.io/test").mock(
            return_value=httpx.Response(404, text="not found")
        )
        with pytest.raises(HarnessAPIError) as exc_info:
            await client._request("GET", "/test")
        assert exc_info.value.status_code == 404


# ------------------------------------------------------------------
# Scope params
# ------------------------------------------------------------------


class TestScopeParams:
    @respx.mock
    @pytest.mark.asyncio
    async def test_scope_params_included(self, client: HarnessClient) -> None:
        route = respx.get("https://app.harness.io/test").mock(
            return_value=httpx.Response(200, json={})
        )
        await client._request("GET", "/test")
        request = route.calls[0].request
        url_params = dict(request.url.params)
        assert url_params["accountIdentifier"] == "acc123"
        assert url_params["orgIdentifier"] == "default"
        assert url_params["projectIdentifier"] == "hawkeye_proj"


# ------------------------------------------------------------------
# Legacy response unwrap
# ------------------------------------------------------------------


class TestLegacyUnwrap:
    def test_unwrap_success_envelope(self, client: HarnessClient) -> None:
        resp = {"status": "SUCCESS", "data": {"key": "value"}}
        assert client._unwrap_legacy(resp) == {"key": "value"}

    def test_passthrough_non_legacy(self, client: HarnessClient) -> None:
        resp = {"identifier": "p1", "name": "Pipeline"}
        assert client._unwrap_legacy(resp) == resp

    def test_passthrough_list(self, client: HarnessClient) -> None:
        resp = [{"identifier": "p1"}]
        assert client._unwrap_legacy(resp) == resp  # type: ignore[arg-type]


class TestLogDownloadHelpers:
    def test_extract_log_key_from_node(self) -> None:
        node = {
            "executableResponses": [
                {"async": {"logKeys": ["acc/pipeline/pipe/1/-exec/stage/step"]}}
            ]
        }
        assert _extract_log_key(node) == "acc/pipeline/pipe/1/-exec/stage/step"

    def test_extract_log_key_from_log_base_key(self) -> None:
        node = {
            "logBaseKey": "acc/pipeline/pipe/1/-exec/stage/step",
        }
        assert _extract_log_key(node) == "acc/pipeline/pipe/1/-exec/stage/step"

    def test_extract_logs_from_zip_ndjson(self) -> None:
        zip_bytes = _make_logs_zip([
            {"timestamp": "2026-05-09T10:00:00Z", "message": "first line"},
            {"log": {"message": "nested line"}},
        ])
        result = _extract_logs_from_zip(zip_bytes)
        assert result is not None
        assert "first line" in result
        assert "nested line" in result


# ------------------------------------------------------------------
# API endpoint methods
# ------------------------------------------------------------------


class TestGetExecution:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_execution(
        self, client: HarnessClient, sample_execution_response: dict
    ) -> None:
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/v2/exec_success_001"
        ).mock(return_value=httpx.Response(200, json=sample_execution_response))

        result = await client.get_execution("exec_success_001")
        assert result.plan_execution_id == "exec_success_001"
        assert result.status == ExecutionStatus.SUCCESS


class TestGetExecutionGraph:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_execution_graph(
        self, client: HarnessClient, sample_execution_graph_response: dict
    ) -> None:
        wrapped = {"status": "SUCCESS", "data": sample_execution_graph_response}
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/getExecutionGraph/exec_001"
        ).mock(return_value=httpx.Response(200, json=wrapped))

        result = await client.get_execution_graph("exec_001")
        assert result.root_node_id == "node_root"
        assert "node_stage_build" in result.nodes


class TestGetStepLogs:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_step_logs_returns_dict(
        self, client: HarnessClient, sample_execution_graph_response: dict
    ) -> None:
        """get_step_logs now returns dict with 'node' and 'subgraph' keys."""
        wrapped = {"status": "SUCCESS", "data": {"executionGraph": sample_execution_graph_response}}
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/v2/exec_1"
        ).mock(return_value=httpx.Response(200, json=wrapped))
        # Mock subgraph call
        respx.get(
            url__regex=r".*/subGraph/exec_1/.*"
        ).mock(return_value=httpx.Response(200, json={"status": "SUCCESS", "data": {}}))

        result = await client.get_step_logs("exec_1", "node_stage_build", "Compile")
        assert isinstance(result, dict)
        assert "node" in result
        assert "subgraph" in result
        assert "api_logs" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_step_logs_step_not_found(
        self, client: HarnessClient, sample_execution_graph_response: dict
    ) -> None:
        wrapped = {"status": "SUCCESS", "data": {"executionGraph": sample_execution_graph_response}}
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/v2/exec_1"
        ).mock(return_value=httpx.Response(200, json=wrapped))

        result = await client.get_step_logs("exec_1", "stage_1", "nonexistent_step")
        assert result["node"] is None
        assert result["subgraph"] is None
        # Must include available nodes for discoverability
        assert "available_nodes" in result
        available = result["available_nodes"]
        assert isinstance(available, list)
        assert len(available) > 0
        # Each entry should have identifier, name, step_type, status
        first = available[0]
        assert "identifier" in first
        assert "name" in first
        assert "step_type" in first
        assert "status" in first

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_step_logs_downloads_api_logs_when_log_key_present(
        self, client: HarnessClient
    ) -> None:
        graph = {
            "status": "SUCCESS",
            "data": {
                "pipelineExecutionSummary": {
                    "planExecutionId": "exec_1",
                    "pipelineIdentifier": "pipe1",
                    "name": "Pipeline",
                    "status": "Success",
                    "runSequence": 7,
                    "layoutNodeMap": {},
                },
                "executionGraph": {
                    "nodeMap": {
                        "node_step": {
                            "name": "Run Tests",
                            "identifier": "run_tests",
                            "stepType": "Run",
                            "status": "Failed",
                            "baseFqn": "pipeline.stages.build.spec.execution.steps.run_tests",
                            "executableResponses": [
                                {
                                    "async": {
                                        "logKeys": [
                                            "acc123/pipeline/pipe1/7/-exec_1/build/run_tests"
                                        ]
                                    }
                                }
                            ],
                        }
                    }
                },
            },
        }
        subgraph = {"status": "SUCCESS", "data": {}}
        zip_bytes = _make_logs_zip([
            {"timestamp": "2026-05-09T10:00:00Z", "message": "pytest failed"},
            {"message": "AssertionError: expected 1"},
        ])

        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/v2/exec_1"
        ).mock(return_value=httpx.Response(200, json=graph))
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/subGraph/exec_1/node_step"
        ).mock(return_value=httpx.Response(200, json=subgraph))
        respx.post(
            "https://app.harness.io/gateway/log-service/blob/download"
        ).mock(
            side_effect=[
                httpx.Response(200, json={"status": "queued", "link": "https://app.harness.io/storage/logs.zip"}),
                httpx.Response(200, json={"status": "success", "link": "https://app.harness.io/storage/logs.zip"}),
            ]
        )
        respx.get("https://app.harness.io/storage/logs.zip").mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

        with patch("providers.harness.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client.get_step_logs("exec_1", "build", "run_tests")

        assert "pytest failed" in result["api_logs"]
        assert "AssertionError" in result["api_logs"]
        assert result["api_log_key"] == "acc123/pipeline/pipe1/7/-exec_1/build/run_tests"

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_step_logs_prefers_failed_duplicate_step_name_for_human_stage_name(
        self, client: HarnessClient
    ) -> None:
        graph = {
            "status": "SUCCESS",
            "data": {
                "pipelineExecutionSummary": {
                    "planExecutionId": "exec_1",
                    "pipelineIdentifier": "pipe1",
                    "name": "Pipeline",
                    "status": "Failed",
                    "runSequence": 7,
                    "layoutNodeMap": {},
                },
                "executionGraph": {
                    "nodeMap": {
                        "node_failed": {
                            "name": "Rollout Deployment",
                            "identifier": "rolloutDeployment",
                            "stepType": "K8sRollingDeploy",
                            "status": "Expired",
                            "baseFqn": "pipeline.stages.deploy_sifi_adapter_k8s.spec.execution.steps.rolloutDeployment",
                            "executableResponses": [
                                {
                                    "async": {
                                        "logKeys": [
                                            "acc123/pipeline/pipe1/7/-exec_1/deploy_sifi_adapter_k8s/rolloutDeployment"
                                        ]
                                    }
                                }
                            ],
                        },
                        "node_rollback": {
                            "name": "Rollout Deployment",
                            "identifier": "rolloutDeployment",
                            "stepType": "K8sRollingDeploy",
                            "status": "Success",
                            "baseFqn": "pipeline.stages.deploy_sifi_adapter_k8s.spec.rollbackSteps.rolloutDeployment",
                        },
                    }
                },
            },
        }
        zip_bytes = _make_logs_zip([
            {"message": "Readiness probe failed"},
            {"message": "Progress deadline exceeded"},
        ])

        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/v2/exec_1"
        ).mock(return_value=httpx.Response(200, json=graph))
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/subGraph/exec_1/node_failed"
        ).mock(return_value=httpx.Response(200, json={"status": "SUCCESS", "data": {}}))
        respx.post(
            "https://app.harness.io/gateway/log-service/blob/download"
        ).mock(
            side_effect=[
                httpx.Response(200, json={"status": "success", "link": "https://app.harness.io/storage/logs.zip"}),
            ]
        )
        respx.get("https://app.harness.io/storage/logs.zip").mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

        result = await client.get_step_logs(
            "exec_1",
            "deploy sifi adapter",
            "Rollout Deployment",
        )

        assert result["node"] is not None
        assert result["node"]["uuid"] == "node_failed"
        assert result["node"]["status"] == "Expired"
        assert "Progress deadline exceeded" in result["api_logs"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_step_logs_downloads_api_logs_from_log_base_key(
        self, client: HarnessClient
    ) -> None:
        graph = {
            "status": "SUCCESS",
            "data": {
                "pipelineExecutionSummary": {
                    "planExecutionId": "exec_1",
                    "pipelineIdentifier": "pipe1",
                    "name": "Pipeline",
                    "status": "Failed",
                    "runSequence": 7,
                    "layoutNodeMap": {},
                },
                "executionGraph": {
                    "nodeMap": {
                        "node_step": {
                            "name": "Rollout Deployment",
                            "identifier": "rolloutDeployment",
                            "stepType": "K8sRollingDeploy",
                            "status": "Failed",
                            "baseFqn": "pipeline.stages.deploy_sifi_adapter.spec.execution.steps.rolloutDeployment",
                            "logBaseKey": "acc123/pipeline/pipe1/7/-exec_1/deploy_sifi_adapter/rolloutDeployment",
                        }
                    }
                },
            },
        }
        zip_bytes = _make_logs_zip([
            {"message": "deployment exceeded progress deadline"},
        ])

        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/v2/exec_1"
        ).mock(return_value=httpx.Response(200, json=graph))
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/subGraph/exec_1/node_step"
        ).mock(return_value=httpx.Response(200, json={"status": "SUCCESS", "data": {}}))
        respx.post(
            "https://app.harness.io/gateway/log-service/blob/download"
        ).mock(
            return_value=httpx.Response(200, json={"status": "success", "link": "https://app.harness.io/storage/logs.zip"})
        )
        respx.get("https://app.harness.io/storage/logs.zip").mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

        result = await client.get_step_logs(
            "exec_1",
            "deploy sifi adapter",
            "node_step",
        )

        assert result["api_log_key"] == "acc123/pipeline/pipe1/7/-exec_1/deploy_sifi_adapter/rolloutDeployment"
        assert "deployment exceeded progress deadline" in result["api_logs"]


class TestListRecentExecutions:
    @respx.mock
    @pytest.mark.asyncio
    async def test_list_recent(self, client: HarnessClient) -> None:
        body = {
            "status": "SUCCESS",
            "data": {
                "content": [
                    {
                        "pipelineExecutionSummary": {
                            "planExecutionId": "ex1",
                            "pipelineIdentifier": "pipe1",
                            "name": "P",
                            "status": "Success",
                            "runSequence": 1,
                        }
                    }
                ]
            },
        }
        respx.post(
            "https://app.harness.io/pipeline/api/pipelines/execution/summary"
        ).mock(return_value=httpx.Response(200, json=body))

        results = await client.list_recent_executions("pipe1", limit=5)
        assert len(results) == 1
        assert results[0].plan_execution_id == "ex1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_with_status_filter(self, client: HarnessClient) -> None:
        respx.post(
            "https://app.harness.io/pipeline/api/pipelines/execution/summary"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"status": "SUCCESS", "data": {"content": []}},
            )
        )

        results = await client.list_recent_executions(
            "pipe1", status=ExecutionStatus.FAILED
        )
        assert results == []


class TestListPipelines:
    @respx.mock
    @pytest.mark.asyncio
    async def test_list_pipelines(
        self, client: HarnessClient, sample_pipeline_list_response: list
    ) -> None:
        respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines"
        ).mock(return_value=httpx.Response(200, json=sample_pipeline_list_response))

        results = await client.list_pipelines()
        assert len(results) == 3
        assert results[0].identifier == "Build_And_Deploy"

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_pipelines_with_search(self, client: HarnessClient) -> None:
        route = respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines"
        ).mock(return_value=httpx.Response(200, json=[]))

        await client.list_pipelines(search="deploy")
        params = dict(route.calls[0].request.url.params)
        assert params["search_term"] == "deploy"


class TestGetInputSets:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_input_sets(
        self, client: HarnessClient, sample_input_sets_response: list
    ) -> None:
        respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/input-sets"
        ).mock(return_value=httpx.Response(200, json=sample_input_sets_response))

        results = await client.get_input_sets("Build_And_Deploy")
        assert len(results) == 2
        assert results[0].identifier == "default_input_set"


class TestGetExecutionInputs:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_execution_inputs(self, client: HarnessClient) -> None:
        wrapped = {
            "status": "SUCCESS",
            "data": {"inputSetYaml": "pipeline:\n  identifier: test\n"},
        }
        respx.get(
            "https://app.harness.io/pipeline/api/pipelines/execution/exec_1/inputsetV2"
        ).mock(return_value=httpx.Response(200, json=wrapped))

        result = await client.get_execution_inputs("exec_1")
        assert "pipeline:" in result


class TestGetPipelineDefinition:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_pipeline_definition(self, client: HarnessClient) -> None:
        respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines/pipe1"
        ).mock(return_value=httpx.Response(200, json={"pipeline_yaml": "yaml content"}))

        result = await client.get_pipeline_definition("pipe1")
        assert result["pipeline_yaml"] == "yaml content"


class TestTestConnection:
    @respx.mock
    @pytest.mark.asyncio
    async def test_connection_success(self, client: HarnessClient) -> None:
        respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines"
        ).mock(return_value=httpx.Response(200, json=[]))

        assert await client.test_connection() is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_connection_failure(self, client: HarnessClient) -> None:
        respx.get(
            "https://app.harness.io/v1/orgs/default/projects/hawkeye_proj/pipelines"
        ).mock(return_value=httpx.Response(401, text="unauthorized"))

        assert await client.test_connection() is False


class TestBaseUrlConfig:
    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_base_url(self, mock_token) -> None:
        profile = HarnessProfile(
            name="custom",
            account_id="acc1",
            org_identifier="org1",
            project_identifier="proj1",
            base_url="https://harness.corp.com",
        )
        c = HarnessClient(profile)
        respx.get("https://harness.corp.com/v1/orgs/org1/projects/proj1/pipelines").mock(
            return_value=httpx.Response(200, json=[])
        )
        results = await c.list_pipelines()
        assert results == []


class TestClientLifecycle:
    @pytest.mark.asyncio
    async def test_close(self, client: HarnessClient) -> None:
        # Closing without opening should not fail
        await client.close()
