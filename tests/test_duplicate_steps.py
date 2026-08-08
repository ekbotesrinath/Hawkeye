"""Regression tests for duplicate step names inside step groups.

Incident (2026-08-06/07): stage ``cd_dev_eus2`` of ``cd_ingestion_function_v2``
contains two step groups, ``deploy_function`` and ``deploy_apim``, each with a
Container step named ``ContainerStep``.  ``hawkeye_get_step_logs`` silently
first-matched every spelling to ``deploy_function/ContainerStep``; the step
inside ``deploy_apim`` was unreachable.  Container steps inside step groups all
get the same default name, so this is a normal Harness pattern.

Covers: group-qualified step addressing, node-UUID addressing, the ambiguity
error with candidate list, stage-detail exposure of addressable IDs, and log
pagination.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models import (
    ExecutionStatus,
    GraphNode,
    HarnessProfile,
    StepExecution,
)
from providers.harness.client import HarnessClient
from providers.harness.graph_search import (
    derive_group_path,
    group_qualifiers_match,
    qualified_step_ref,
    select_step_node,
    split_step_path,
)

_NOW_MS = int(time.time() * 1000)

_EXEC_ID = "6dV3V1b_RNOGzLTBVjhMXQ"
_STAGE = "cd_dev_eus2"
_FQN_PREFIX = f"pipeline.stages.{_STAGE}.spec.execution.steps"
_LOG_KEY_PREFIX = f"acc123/pipeline/cd_ingestion_function_v2/12/-{_EXEC_ID}/{_STAGE}"


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def profile() -> HarnessProfile:
    return HarnessProfile(
        name="test-profile",
        account_id="acc123",
        org_identifier="platform_services",
        project_identifier="Gail",
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


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.list_pipelines.return_value = []

    async def _get_provider():
        return provider

    with (
        patch("tools.executions.get_provider", side_effect=_get_provider),
        patch("tools._helpers.get_provider", side_effect=_get_provider),
    ):
        yield provider


@pytest.fixture()
def mock_audit():
    with patch("tools._helpers.log_tool_call") as mock:
        yield mock


def _container_node(group: str, status: str) -> dict:
    return {
        "name": "ContainerStep",
        "identifier": "ContainerStep",
        "stepType": "Container",
        "status": status,
        "startTs": _NOW_MS - 120_000,
        "endTs": _NOW_MS - 60_000,
        "baseFqn": f"{_FQN_PREFIX}.{group}.steps.ContainerStep",
        "executableResponses": [
            {"async": {"logKeys": [f"{_LOG_KEY_PREFIX}/{group}/ContainerStep"]}}
        ],
    }


def _incident_graph(fn_status: str = "Success", apim_status: str = "Success") -> dict:
    """The cd_dev_eus2 stage: two step groups, one ContainerStep each."""
    node_map = {
        "uuid_group_fn": {
            "name": "deploy_function",
            "identifier": "deploy_function",
            "stepType": "STEP_GROUP",
            "status": fn_status,
            "baseFqn": f"{_FQN_PREFIX}.deploy_function",
        },
        "uuid_group_apim": {
            "name": "deploy_apim",
            "identifier": "deploy_apim",
            "stepType": "STEP_GROUP",
            "status": apim_status,
            "baseFqn": f"{_FQN_PREFIX}.deploy_apim",
        },
        "uuid_ctr_fn": _container_node("deploy_function", fn_status),
        "uuid_ctr_apim": _container_node("deploy_apim", apim_status),
    }
    return {
        "status": "SUCCESS",
        "data": {
            "pipelineExecutionSummary": {
                "planExecutionId": _EXEC_ID,
                "pipelineIdentifier": "cd_ingestion_function_v2",
                "name": "cd_ingestion_function_v2",
                "status": "Success",
                "runSequence": 12,
                "layoutNodeMap": {},
            },
            "executionGraph": {"nodeMap": node_map},
        },
    }


def _mock_incident_routes(graph: dict) -> None:
    respx.get(
        f"https://app.harness.io/pipeline/api/pipelines/execution/v2/{_EXEC_ID}"
    ).mock(return_value=httpx.Response(200, json=graph))
    respx.get(url__regex=rf".*/subGraph/{_EXEC_ID}/.*").mock(
        return_value=httpx.Response(200, json={"status": "SUCCESS", "data": {}})
    )
    # Log Service declines — keeps the test focused on node resolution;
    # api_log_key still proves which node was addressed.
    respx.post("https://app.harness.io/gateway/log-service/blob/download").mock(
        return_value=httpx.Response(200, json={"status": "error"})
    )


# ------------------------------------------------------------------
# Client: duplicate ContainerStep resolution (the incident)
# ------------------------------------------------------------------


class TestDuplicateStepResolution:
    @respx.mock
    @pytest.mark.asyncio
    async def test_bare_duplicate_name_errors_with_candidates(
        self, client: HarnessClient
    ) -> None:
        """An ambiguous step_id must list candidates, never silently pick one."""
        _mock_incident_routes(_incident_graph())

        result = await client.get_step_logs(_EXEC_ID, _STAGE, "ContainerStep")

        assert result["node"] is None
        candidates = result["ambiguous_matches"]
        assert len(candidates) == 2
        qualified = {c["qualified_id"] for c in candidates}
        assert qualified == {
            "deploy_function/ContainerStep",
            "deploy_apim/ContainerStep",
        }
        uuids = {c["uuid"] for c in candidates}
        assert uuids == {"uuid_ctr_fn", "uuid_ctr_apim"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_group_qualified_path_reaches_each_twin(
        self, client: HarnessClient
    ) -> None:
        """`<group>/<step>` addresses the step inside that specific group."""
        _mock_incident_routes(_incident_graph())

        for group, uuid in (
            ("deploy_apim", "uuid_ctr_apim"),
            ("deploy_function", "uuid_ctr_fn"),
        ):
            result = await client.get_step_logs(
                _EXEC_ID, _STAGE, f"{group}/ContainerStep"
            )
            assert result["node"] is not None
            assert result["node"]["uuid"] == uuid
            # The log key is the incident's smoking gun: it must end with the
            # requested group, not always /deploy_function/.
            assert result["api_log_key"] == f"{_LOG_KEY_PREFIX}/{group}/ContainerStep"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stage_prefixed_qualified_path_also_works(
        self, client: HarnessClient
    ) -> None:
        _mock_incident_routes(_incident_graph())

        result = await client.get_step_logs(
            _EXEC_ID, _STAGE, f"{_STAGE}/deploy_apim/ContainerStep"
        )
        assert result["node"] is not None
        assert result["node"]["uuid"] == "uuid_ctr_apim"

    @respx.mock
    @pytest.mark.asyncio
    async def test_node_uuid_addresses_exactly_one_step(
        self, client: HarnessClient
    ) -> None:
        """The runtime node UUID from get_stage_detail is accepted as step_id."""
        _mock_incident_routes(_incident_graph())

        result = await client.get_step_logs(_EXEC_ID, _STAGE, "uuid_ctr_apim")
        assert result["node"] is not None
        assert result["node"]["uuid"] == "uuid_ctr_apim"

    @respx.mock
    @pytest.mark.asyncio
    async def test_execution_status_does_not_break_ambiguity(
        self, client: HarnessClient
    ) -> None:
        """A Failed twin must NOT be silently preferred over a Success twin.

        That heuristic is exactly what returned plausible-looking logs from
        the wrong step during the incident.
        """
        _mock_incident_routes(_incident_graph(fn_status="Failed", apim_status="Success"))

        result = await client.get_step_logs(_EXEC_ID, _STAGE, "ContainerStep")
        assert result["node"] is None
        assert len(result["ambiguous_matches"]) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_not_found_lists_qualified_ids(self, client: HarnessClient) -> None:
        """available_nodes now carry qualified_id + uuid so callers can retry."""
        _mock_incident_routes(_incident_graph())

        result = await client.get_step_logs(_EXEC_ID, _STAGE, "no_such_step")
        assert result["node"] is None
        available = result["available_nodes"]
        qualified = {n["qualified_id"] for n in available}
        assert "deploy_apim/ContainerStep" in qualified
        assert all("uuid" in n for n in available)


# ------------------------------------------------------------------
# graph_search unit tests
# ------------------------------------------------------------------


class TestStepPathHelpers:
    def test_split_step_path_unqualified(self) -> None:
        assert split_step_path("ContainerStep") == ([], "ContainerStep")

    def test_split_step_path_qualified(self) -> None:
        assert split_step_path("deploy_apim/ContainerStep") == (
            ["deploy_apim"],
            "ContainerStep",
        )

    def test_split_step_path_multi_level(self) -> None:
        assert split_step_path("cd_dev_eus2/deploy_apim/ContainerStep") == (
            ["cd_dev_eus2", "deploy_apim"],
            "ContainerStep",
        )

    def test_group_qualifiers_match_is_fuzzy(self) -> None:
        fqn = f"{_FQN_PREFIX}.deploy_apim.steps.ContainerStep"
        assert group_qualifiers_match(["deploy_apim"], fqn)
        assert group_qualifiers_match(["deploy-apim"], fqn)
        assert group_qualifiers_match(["DeployApim"], fqn)
        assert not group_qualifiers_match(["deploy_function"], fqn)
        assert not group_qualifiers_match(["deploy_apim"], None)

    def test_derive_group_path_inside_group(self) -> None:
        fqn = f"{_FQN_PREFIX}.deploy_apim.steps.ContainerStep"
        assert derive_group_path(fqn, "ContainerStep") == "deploy_apim"

    def test_derive_group_path_top_level_step(self) -> None:
        fqn = "pipeline.stages.Build.spec.execution.steps.Compile"
        assert derive_group_path(fqn, "Compile") == ""

    def test_derive_group_path_rollback_step(self) -> None:
        fqn = "pipeline.stages.deploy.spec.rollbackSteps.rolloutDeployment"
        assert derive_group_path(fqn, "rolloutDeployment") == "rollbackSteps"

    def test_derive_group_path_no_stages_segment(self) -> None:
        assert derive_group_path("pipeline", "x") == ""
        assert derive_group_path(None, "x") == ""

    def test_qualified_step_ref(self) -> None:
        fqn = f"{_FQN_PREFIX}.deploy_apim.steps.ContainerStep"
        assert qualified_step_ref(fqn, "ContainerStep") == "deploy_apim/ContainerStep"
        top = "pipeline.stages.Build.spec.execution.steps.Compile"
        assert qualified_step_ref(top, "Compile") == "Compile"


def _graph_node(uuid: str, base_fqn: str, status: ExecutionStatus) -> GraphNode:
    return GraphNode(
        uuid=uuid,
        name="Rollout Deployment",
        identifier="rolloutDeployment",
        step_type="K8sRollingDeploy",
        status=status,
        base_fqn=base_fqn,
    )


class TestSelectStepNode:
    def test_main_path_beats_rollback_twin(self) -> None:
        """The historical deploy/rollback same-name pattern stays resolvable."""
        main = _graph_node(
            "u_main",
            "pipeline.stages.d.spec.execution.steps.rolloutDeployment",
            ExecutionStatus.FAILED,
        )
        rollback = _graph_node(
            "u_rb",
            "pipeline.stages.d.spec.rollbackSteps.rolloutDeployment",
            ExecutionStatus.SUCCESS,
        )
        match, ambiguous = select_step_node(
            [("u_main", main), ("u_rb", rollback)], "Rollout Deployment"
        )
        assert ambiguous == []
        assert match is not None
        assert match[0] == "u_main"

    def test_uuid_tier_wins_over_name_duplicates(self) -> None:
        a = _graph_node(
            "u_a", "pipeline.stages.d.spec.execution.steps.g1.steps.rolloutDeployment",
            ExecutionStatus.SUCCESS,
        )
        b = _graph_node(
            "u_b", "pipeline.stages.d.spec.execution.steps.g2.steps.rolloutDeployment",
            ExecutionStatus.SUCCESS,
        )
        match, ambiguous = select_step_node([("u_a", a), ("u_b", b)], "u_b")
        assert ambiguous == []
        assert match is not None
        assert match[0] == "u_b"

    def test_two_main_path_twins_are_ambiguous(self) -> None:
        a = _graph_node(
            "u_a", "pipeline.stages.d.spec.execution.steps.g1.steps.rolloutDeployment",
            ExecutionStatus.FAILED,
        )
        b = _graph_node(
            "u_b", "pipeline.stages.d.spec.execution.steps.g2.steps.rolloutDeployment",
            ExecutionStatus.SUCCESS,
        )
        match, ambiguous = select_step_node([("u_a", a), ("u_b", b)], "rolloutDeployment")
        assert match is None
        assert {u for u, _n in ambiguous} == {"u_a", "u_b"}

    def test_empty_pool(self) -> None:
        assert select_step_node([], "x") == (None, [])


# ------------------------------------------------------------------
# Tool level: ambiguity rendering, stage detail, log pagination
# ------------------------------------------------------------------


def _ambiguous_candidate(group: str, uuid: str, status: str = "Success") -> dict:
    return {
        "uuid": uuid,
        "identifier": "ContainerStep",
        "name": "ContainerStep",
        "qualified_id": f"{group}/ContainerStep",
        "step_type": "Container",
        "status": status,
        "base_fqn": f"{_FQN_PREFIX}.{group}.steps.ContainerStep",
    }


class TestGetStepLogsToolAmbiguity:
    @pytest.mark.asyncio
    async def test_ambiguous_step_renders_candidate_list(
        self, mock_provider, mock_audit
    ) -> None:
        mock_provider.get_step_logs.return_value = {
            "node": None,
            "subgraph": None,
            "ambiguous_matches": [
                _ambiguous_candidate("deploy_function", "uuid_ctr_fn"),
                _ambiguous_candidate("deploy_apim", "uuid_ctr_apim"),
            ],
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(
            execution_id=_EXEC_ID, stage_id=_STAGE, step_id="ContainerStep"
        )
        assert "Ambiguous" in result
        assert "deploy_function/ContainerStep" in result
        assert "deploy_apim/ContainerStep" in result
        assert "uuid_ctr_fn" in result
        assert "uuid_ctr_apim" in result
        # Must instruct how to disambiguate
        assert "<step_group>/<step>" in result


class TestStageDetailExposesAddresses:
    def test_qualified_step_id_and_node_uuid_shown(self) -> None:
        from tests.test_executions import _make_execution, _make_stage
        from tools.executions import _format_stage_detail

        twins = [
            StepExecution(
                step_id="uuid_ctr_fn",
                name="ContainerStep",
                identifier="ContainerStep",
                status=ExecutionStatus.SUCCESS,
                step_type="Container",
                group_path="deploy_function",
            ),
            StepExecution(
                step_id="uuid_ctr_apim",
                name="ContainerStep",
                identifier="ContainerStep",
                status=ExecutionStatus.SUCCESS,
                step_type="Container",
                group_path="deploy_apim",
            ),
        ]
        stage = _make_stage(stage_id=_STAGE, name="cd_dev_eus2", steps=twins)
        ex = _make_execution(stages=[stage])
        result = _format_stage_detail(ex, stage)

        # Both twins individually addressable straight from this output
        assert "`deploy_function/ContainerStep`" in result
        assert "`deploy_apim/ContainerStep`" in result
        assert "`uuid_ctr_fn`" in result
        assert "`uuid_ctr_apim`" in result
        assert "hawkeye_get_step_logs" in result


class TestGetStepLogsPagination:
    def _step_logs_result(self, api_logs: str) -> dict:
        return {
            "node": {
                "name": "ContainerStep",
                "stepType": "Container",
                "status": "Success",
                "startTs": _NOW_MS - 60_000,
                "endTs": _NOW_MS,
            },
            "subgraph": {},
            "api_logs": api_logs,
        }

    def _long_log(self) -> str:
        # The content that mattered in the incident (serviceUrl from
        # `az apim api import`) sat near the BEGINNING of a long log.
        lines = ['{"serviceUrl": "https://func-ingestion-dev.azurewebsites.net"}']
        lines += [f"progress tick {i}" for i in range(400)]
        return "\n".join(lines)

    @pytest.mark.asyncio
    async def test_default_tail_states_truncation(
        self, mock_provider, mock_audit
    ) -> None:
        mock_provider.get_step_logs.return_value = self._step_logs_result(
            self._long_log()
        )
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1", step_id="st1")
        assert "earlier lines omitted" in result
        assert "log_start=1" in result
        assert "serviceUrl" not in result  # tail view — start of log not shown

    @pytest.mark.asyncio
    async def test_log_start_reaches_beginning_of_long_log(
        self, mock_provider, mock_audit
    ) -> None:
        mock_provider.get_step_logs.return_value = self._step_logs_result(
            self._long_log()
        )
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(
            execution_id="e1", stage_id="s1", step_id="st1", log_start=1
        )
        assert "serviceUrl" in result  # acceptance criterion 3
        assert "later lines omitted" in result
        assert "log_start=101" in result

    @pytest.mark.asyncio
    async def test_short_log_not_annotated(self, mock_provider, mock_audit) -> None:
        mock_provider.get_step_logs.return_value = self._step_logs_result(
            "only line one\nonly line two"
        )
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1", step_id="st1")
        assert "only line one" in result
        assert "omitted" not in result


class TestListRecentExecutionsStatusValidation:
    @pytest.mark.asyncio
    async def test_invalid_status_rejected_before_any_api_call(
        self, mock_provider, mock_audit
    ) -> None:
        """A failure TYPE passed as a status must not reach Harness (Bug 2)."""
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_recent_executions"].fn
        result = await tool_fn(
            pipeline_id="cd_search_schema_provisioner",
            status="INFRASTRUCTURE_FAILURE",
        )
        assert "not a valid execution status" in result
        assert "Failed" in result  # valid values are listed
        mock_provider.list_recent_executions.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_status_passes_through(
        self, mock_provider, mock_audit
    ) -> None:
        mock_provider.list_recent_executions.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_recent_executions"].fn
        result = await tool_fn(
            pipeline_id="cd_search_schema_provisioner", status="Failed"
        )
        assert "No executions found" in result
        mock_provider.list_recent_executions.assert_awaited_once()
        _args, kwargs = mock_provider.list_recent_executions.call_args
        assert kwargs["status"] == ExecutionStatus.FAILED
