"""Tests for providers.harness.parser — response parsing utilities."""

from __future__ import annotations

from core.models import (
    ExecutionGraph,
    ExecutionStatus,
    PipelineExecution,
    PipelineSummary,
)
from providers.harness.parser import (
    build_execution_url,
    parse_execution_detail,
    parse_execution_graph,
    parse_input_sets,
    parse_pipeline_list,
    parse_recent_executions,
    parse_template_list,
    _extract_steps_for_stage,
)

# ------------------------------------------------------------------
# parse_execution_detail
# ------------------------------------------------------------------


class TestParseExecutionDetail:
    def test_parse_success_execution(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(
            data, base_url="https://app.harness.io",
            account_id="acc1", org="default", project="proj",
        )
        assert isinstance(result, PipelineExecution)
        assert result.plan_execution_id == "exec_success_001"
        assert result.status == ExecutionStatus.SUCCESS
        assert result.pipeline_name == "Build And Deploy"
        assert result.run_sequence == 42

    def test_parse_failed_execution(self, sample_execution_failed_response: dict) -> None:
        data = sample_execution_failed_response["data"]
        result = parse_execution_detail(data)
        assert result.status == ExecutionStatus.FAILED
        assert result.plan_execution_id == "exec_failed_002"
        assert result.trigger_type == "WEBHOOK"

    def test_parse_stages(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(data)
        assert len(result.stages) == 2
        assert result.stages[0].name == "Build"
        assert result.stages[1].name == "Deploy to Prod"

    def test_parse_trigger_info(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(data)
        assert result.trigger_type == "MANUAL"
        assert result.triggered_by == "srinath.ekbote"

    def test_parse_git_details(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(data)
        assert result.branch == "main"

    def test_parse_tags(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(data)
        assert result.tags == {"env": "prod", "team": "sre"}

    def test_parse_with_no_git_details(self) -> None:
        data = {
            "pipelineExecutionSummary": {
                "planExecutionId": "ex1",
                "pipelineIdentifier": "p1",
                "name": "P",
                "status": "Running",
                "layoutNodeMap": {},
            },
            "executionGraph": {"nodeMap": {}},
        }
        result = parse_execution_detail(data)
        assert result.branch is None

    def test_parse_with_empty_tags(self) -> None:
        data = {
            "pipelineExecutionSummary": {
                "planExecutionId": "ex1",
                "pipelineIdentifier": "p1",
                "name": "P",
                "status": "Success",
                "tags": [],
                "layoutNodeMap": {},
            },
            "executionGraph": {"nodeMap": {}},
        }
        result = parse_execution_detail(data)
        assert result.tags == {}

    def test_parse_unknown_status(self) -> None:
        data = {
            "pipelineExecutionSummary": {
                "planExecutionId": "ex1",
                "pipelineIdentifier": "p1",
                "name": "P",
                "status": "SomeNewStatus",
                "layoutNodeMap": {},
            },
            "executionGraph": {"nodeMap": {}},
        }
        result = parse_execution_detail(data)
        assert result.status == ExecutionStatus.UNKNOWN

    def test_parse_execution_url_built(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(
            data, base_url="https://app.harness.io",
            account_id="acc1", org="default", project="proj",
        )
        assert "acc1" in result.execution_url
        assert "exec_success_001" in result.execution_url

    def test_parse_failed_stage_failure_info(self, sample_execution_failed_response: dict) -> None:
        data = sample_execution_failed_response["data"]
        result = parse_execution_detail(data)
        build_stage = result.stages[0]
        assert build_stage.failure_info is not None
        assert "compilation error" in build_stage.failure_info.message

    def test_parse_steps_in_stages(self, sample_execution_response: dict) -> None:
        data = sample_execution_response["data"]
        result = parse_execution_detail(data)
        build_stage = result.stages[0]
        # Steps are extracted from nodeMap based on parentStepId
        assert len(build_stage.steps) >= 1

    def test_parse_parallel_stages(self) -> None:
        """Parallel stage groups use currentNodeChildren; all child stages must appear."""
        data = {
            "pipelineExecutionSummary": {
                "planExecutionId": "exec_parallel_001",
                "pipelineIdentifier": "deploy_pipeline",
                "name": "Deploy Pipeline",
                "status": "Success",
                "layoutNodeMap": {
                    "stage_build": {
                        "name": "Build",
                        "nodeIdentifier": "Build",
                        "status": "Success",
                        "nodeType": "CI",
                        "startTs": 1700000000000,
                        "endTs": 1700000300000,
                        "edgeLayoutList": {"nextIds": ["parallel_group_1"]},
                    },
                    "parallel_group_1": {
                        "name": "",
                        "nodeIdentifier": "parallel_group_1",
                        "status": "Success",
                        "nodeType": "parallel",
                        "edgeLayoutList": {
                            "currentNodeChildren": [
                                "stage_deploy_svc_a",
                                "stage_deploy_svc_b",
                                "stage_deploy_svc_c",
                            ],
                            "nextIds": ["stage_notify"],
                        },
                    },
                    "stage_deploy_svc_a": {
                        "name": "deploy svc-a",
                        "nodeIdentifier": "deploy_svc_a",
                        "status": "Success",
                        "nodeType": "CD",
                        "startTs": 1700000300000,
                        "endTs": 1700000500000,
                        "edgeLayoutList": {"nextIds": []},
                    },
                    "stage_deploy_svc_b": {
                        "name": "deploy svc-b",
                        "nodeIdentifier": "deploy_svc_b",
                        "status": "Skipped",
                        "nodeType": "CD",
                        "edgeLayoutList": {"nextIds": []},
                    },
                    "stage_deploy_svc_c": {
                        "name": "deploy svc-c",
                        "nodeIdentifier": "deploy_svc_c",
                        "status": "Success",
                        "nodeType": "CD",
                        "startTs": 1700000300000,
                        "endTs": 1700000450000,
                        "edgeLayoutList": {"nextIds": []},
                    },
                    "stage_notify": {
                        "name": "Notify",
                        "nodeIdentifier": "notify",
                        "status": "Skipped",
                        "nodeType": "Custom",
                        "edgeLayoutList": {"nextIds": []},
                    },
                },
                "startingNodeId": "stage_build",
            },
            "executionGraph": {"nodeMap": {}},
        }
        result = parse_execution_detail(data)
        names = [s.name for s in result.stages]
        # The parallel wrapper should NOT appear as a stage
        assert "" not in names
        # All three child stages must appear
        assert "deploy svc-a" in names
        assert "deploy svc-b" in names
        assert "deploy svc-c" in names
        # Sequential stages still appear
        assert "Build" in names
        assert "Notify" in names
        # Total = Build + 3 parallel children + Notify = 5
        assert len(result.stages) == 5

    def test_parse_nested_parallel_groups(self) -> None:
        """Two sequential parallel groups are both expanded."""
        data = {
            "pipelineExecutionSummary": {
                "planExecutionId": "exec_nested_001",
                "pipelineIdentifier": "multi_parallel",
                "name": "Multi Parallel",
                "status": "Success",
                "layoutNodeMap": {
                    "pg1": {
                        "name": "",
                        "nodeIdentifier": "pg1",
                        "status": "Success",
                        "nodeType": "parallel",
                        "edgeLayoutList": {
                            "currentNodeChildren": ["stage_a", "stage_b"],
                            "nextIds": ["pg2"],
                        },
                    },
                    "stage_a": {
                        "name": "A",
                        "nodeIdentifier": "a",
                        "status": "Success",
                        "nodeType": "CD",
                        "edgeLayoutList": {"nextIds": []},
                    },
                    "stage_b": {
                        "name": "B",
                        "nodeIdentifier": "b",
                        "status": "Skipped",
                        "nodeType": "CD",
                        "edgeLayoutList": {"nextIds": []},
                    },
                    "pg2": {
                        "name": "",
                        "nodeIdentifier": "pg2",
                        "status": "Success",
                        "nodeType": "parallel",
                        "edgeLayoutList": {
                            "currentNodeChildren": ["stage_c"],
                            "nextIds": [],
                        },
                    },
                    "stage_c": {
                        "name": "C",
                        "nodeIdentifier": "c",
                        "status": "Success",
                        "nodeType": "CD",
                        "edgeLayoutList": {"nextIds": []},
                    },
                },
                "startingNodeId": "pg1",
            },
            "executionGraph": {"nodeMap": {}},
        }
        result = parse_execution_detail(data)
        names = [s.name for s in result.stages]
        assert names == ["A", "B", "C"]


# ------------------------------------------------------------------
# parse_execution_graph
# ------------------------------------------------------------------


class TestParseExecutionGraph:
    def test_parse_graph(self, sample_execution_graph_response: dict) -> None:
        result = parse_execution_graph(sample_execution_graph_response)
        assert isinstance(result, ExecutionGraph)
        assert result.root_node_id == "node_root"
        assert len(result.nodes) == 7

    def test_graph_node_fields(self, sample_execution_graph_response: dict) -> None:
        result = parse_execution_graph(sample_execution_graph_response)
        compile_node = result.nodes["node_step_compile"]
        assert compile_node.name == "Compile"
        assert compile_node.step_type == "Run"
        assert compile_node.status == ExecutionStatus.SUCCESS
        assert compile_node.base_fqn is not None

    def test_graph_node_with_outcomes(self, sample_execution_graph_response: dict) -> None:
        result = parse_execution_graph(sample_execution_graph_response)
        compile_node = result.nodes["node_step_compile"]
        assert compile_node.outcomes is not None

    def test_parallel_node(self, sample_execution_graph_response: dict) -> None:
        result = parse_execution_graph(sample_execution_graph_response)
        parallel = result.nodes["node_parallel_group"]
        assert parallel.step_type == "FORK"

    def test_empty_node_map(self) -> None:
        result = parse_execution_graph({"nodeMap": {}, "rootNodeId": ""})
        assert result.nodes == {}
        assert result.root_node_id == ""

    def test_nested_execution_graph_key(self) -> None:
        data = {
            "executionGraph": {
                "nodeMap": {
                    "n1": {
                        "name": "Step1",
                        "identifier": "step1",
                        "stepType": "Run",
                        "status": "Success",
                    }
                }
            },
            "rootNodeId": "n1",
        }
        result = parse_execution_graph(data)
        assert "n1" in result.nodes

    def test_unknown_status_in_graph(self) -> None:
        data = {
            "nodeMap": {
                "n1": {
                    "name": "X",
                    "identifier": "x",
                    "stepType": "Custom",
                    "status": "WeirdStatus",
                }
            },
            "rootNodeId": "n1",
        }
        result = parse_execution_graph(data)
        assert result.nodes["n1"].status == ExecutionStatus.UNKNOWN


# ------------------------------------------------------------------
# parse_pipeline_list
# ------------------------------------------------------------------


class TestParsePipelineList:
    def test_parse_list(self, sample_pipeline_list_response: list) -> None:
        result = parse_pipeline_list(sample_pipeline_list_response)
        assert len(result) == 3
        assert all(isinstance(p, PipelineSummary) for p in result)

    def test_first_pipeline_fields(self, sample_pipeline_list_response: list) -> None:
        result = parse_pipeline_list(sample_pipeline_list_response)
        p = result[0]
        assert p.identifier == "Build_And_Deploy"
        assert p.name == "Build And Deploy"
        assert p.modules == ["CI", "CD"]
        assert p.store_type == "REMOTE"
        assert p.repo_name == "hawkeye"

    def test_last_execution_status(self, sample_pipeline_list_response: list) -> None:
        result = parse_pipeline_list(sample_pipeline_list_response)
        assert result[0].last_execution_status == ExecutionStatus.SUCCESS
        assert result[1].last_execution_status == ExecutionStatus.FAILED

    def test_no_recent_execution(self, sample_pipeline_list_response: list) -> None:
        result = parse_pipeline_list(sample_pipeline_list_response)
        # Third pipeline has empty recent_execution_info
        assert result[2].last_execution_status is None

    def test_empty_list(self) -> None:
        result = parse_pipeline_list([])
        assert result == []


# ------------------------------------------------------------------
# parse_input_sets
# ------------------------------------------------------------------


class TestParseInputSets:
    def test_parse_input_sets(self, sample_input_sets_response: list) -> None:
        result = parse_input_sets(sample_input_sets_response)
        assert len(result) == 2
        assert result[0].identifier == "default_input_set"
        assert result[0].pipeline_identifier == "Build_And_Deploy"

    def test_input_set_yaml_content(self, sample_input_sets_response: list) -> None:
        result = parse_input_sets(sample_input_sets_response)
        assert "inputSet:" in result[0].input_set_yaml

    def test_empty_input_sets(self) -> None:
        assert parse_input_sets([]) == []


# ------------------------------------------------------------------
# parse_template_list
# ------------------------------------------------------------------


class TestParseTemplateList:
    def test_parse_list(self, sample_template_list_response: dict) -> None:
        items = sample_template_list_response["data"]["content"]
        result = parse_template_list(items)
        assert len(result) == 3

    def test_first_template_fields(self, sample_template_list_response: dict) -> None:
        items = sample_template_list_response["data"]["content"]
        result = parse_template_list(items)
        t = result[0]
        assert t.identifier == "deploy_step_template"
        assert t.name == "Deploy Step Template"
        assert t.version_label == "v2"
        assert t.template_entity_type == "Step"
        assert t.child_type == "K8sRollingDeploy"
        assert t.scope == "project"
        assert t.stable_template is True
        assert t.tags == {"team": "platform"}
        assert t.store_type == "INLINE"
        assert t.last_updated == 1700000000000

    def test_org_scope_and_remote_store(
        self, sample_template_list_response: dict
    ) -> None:
        items = sample_template_list_response["data"]["content"]
        result = parse_template_list(items)
        t = result[2]
        assert t.scope == "org"
        assert t.store_type == "REMOTE"
        assert t.child_type is None

    def test_scope_inferred_when_template_scope_missing(self) -> None:
        items = [
            {"identifier": "acct_tmpl", "name": "A"},
            {"identifier": "org_tmpl", "name": "B", "orgIdentifier": "default"},
            {
                "identifier": "proj_tmpl",
                "name": "C",
                "orgIdentifier": "default",
                "projectIdentifier": "proj",
            },
        ]
        result = parse_template_list(items)
        assert result[0].scope == "account"
        assert result[1].scope == "org"
        assert result[2].scope == "project"

    def test_missing_fields_use_defaults(self) -> None:
        result = parse_template_list([{"identifier": "t1"}])
        t = result[0]
        assert t.name == ""
        assert t.version_label == ""
        assert t.stable_template is False
        assert t.tags == {}
        assert t.store_type == "INLINE"
        assert t.last_updated is None

    def test_non_dict_entries_skipped(self) -> None:
        result = parse_template_list(["garbage", {"identifier": "t1", "name": "T1"}])  # type: ignore[list-item]
        assert len(result) == 1
        assert result[0].identifier == "t1"

    def test_empty_list(self) -> None:
        assert parse_template_list([]) == []


# ------------------------------------------------------------------
# parse_recent_executions
# ------------------------------------------------------------------


class TestParseRecentExecutions:
    def test_parse_content_list(self) -> None:
        data = {
            "content": [
                {
                    "pipelineExecutionSummary": {
                        "planExecutionId": "ex1",
                        "pipelineIdentifier": "p1",
                        "name": "P",
                        "status": "Success",
                        "runSequence": 5,
                    }
                }
            ]
        }
        result = parse_recent_executions(data)
        assert len(result) == 1
        assert result[0].plan_execution_id == "ex1"

    def test_parse_empty(self) -> None:
        result = parse_recent_executions({"content": []})
        assert result == []

    def test_parse_direct_list(self) -> None:
        data = [
            {
                "pipelineExecutionSummary": {
                    "planExecutionId": "ex2",
                    "pipelineIdentifier": "p2",
                    "name": "P2",
                    "status": "Failed",
                }
            }
        ]
        result = parse_recent_executions(data)
        assert result[0].status == ExecutionStatus.FAILED

    def test_parse_stages_from_layout_node_map(self) -> None:
        """Summary items carry layoutNodeMap — stages must parse from it so
        history-based analysis (failure hotspots, same-stage detection) works."""
        data = {
            "content": [
                {
                    "pipelineExecutionSummary": {
                        "planExecutionId": "ex3",
                        "pipelineIdentifier": "p3",
                        "name": "P3",
                        "status": "Failed",
                        "startingNodeId": "n1",
                        "layoutNodeMap": {
                            "n1": {
                                "nodeIdentifier": "build",
                                "name": "Build",
                                "status": "Success",
                                "nodeType": "CI",
                                "edgeLayoutList": {"nextIds": ["n2"]},
                            },
                            "n2": {
                                "nodeIdentifier": "deploy",
                                "name": "Deploy",
                                "status": "Failed",
                                "nodeType": "Deployment",
                                "edgeLayoutList": {"nextIds": []},
                            },
                        },
                    }
                }
            ]
        }
        result = parse_recent_executions(data)
        stages = result[0].stages
        assert [s.name for s in stages] == ["Build", "Deploy"]
        assert stages[1].status == ExecutionStatus.FAILED
        # No executionGraph in summary items — steps stay empty
        assert stages[1].steps == []


# ------------------------------------------------------------------
# build_execution_url
# ------------------------------------------------------------------


class TestBuildExecutionUrl:
    def test_build_full_url(self) -> None:
        url = build_execution_url(
            "https://app.harness.io", "acc1", "org1", "proj1", "pipe1", "exec1"
        )
        assert url == (
            "https://app.harness.io/ng/account/acc1/module/cd"
            "/orgs/org1/projects/proj1"
            "/pipelines/pipe1/executions/exec1/pipeline"
        )

    def test_build_url_strips_trailing_slash(self) -> None:
        url = build_execution_url(
            "https://app.harness.io/", "acc1", "org1", "proj1", "pipe1", "exec1"
        )
        assert "harness.io//ng" not in url

    def test_build_url_missing_params_returns_empty(self) -> None:
        assert build_execution_url("", "acc", "org", "proj", "pipe", "exec") == ""
        assert build_execution_url("url", "", "org", "proj", "pipe", "exec") == ""

    def test_build_url_custom_domain(self) -> None:
        url = build_execution_url(
            "https://harness.corp.com", "acc1", "org1", "proj1", "pipe1", "exec1"
        )
        assert url.startswith("https://harness.corp.com/ng/account/")

    def test_build_url_with_module(self) -> None:
        url = build_execution_url(
            "https://app.harness.io", "acc1", "org1", "proj1", "pipe1", "exec1",
            module="ci",
        )
        assert "/module/ci/" in url
        assert "/module/cd/" not in url


# ------------------------------------------------------------------
# _extract_steps_for_stage — step matching logic
# ------------------------------------------------------------------


class TestExtractStepsForStage:
    """Verify step extraction matches by identifier in baseFqn, not only by UUID."""

    def test_matches_by_identifier_in_base_fqn(self) -> None:
        """Steps whose baseFqn contains 'pipeline.stages.<identifier>.' should be found."""
        node_map = {
            "uuid-step-1": {
                "identifier": "dotnet_build_sonar",
                "name": "dotnet_build_sonar",
                "stepType": "Run",
                "status": "Failed",
                "baseFqn": "pipeline.stages.run_sonarqube.spec.execution.steps.dotnet_build_test_analyze.steps.dotnet_build_sonar",
                "parentStepId": "uuid-group-1",
            },
        }
        # stage_id is a UUID that does NOT appear in baseFqn
        steps = _extract_steps_for_stage("uuid-stage-xyz", node_map, "run_sonarqube")
        assert len(steps) == 1
        assert steps[0].identifier == "dotnet_build_sonar"

    def test_no_match_without_identifier(self) -> None:
        """Without stage_identifier, UUID-only matching may fail for CI pipelines."""
        node_map = {
            "uuid-step-1": {
                "identifier": "dotnet_build_sonar",
                "name": "dotnet_build_sonar",
                "stepType": "Run",
                "status": "Failed",
                "baseFqn": "pipeline.stages.run_sonarqube.spec.execution.steps.dotnet_build_sonar",
                "parentStepId": "uuid-group-1",
            },
        }
        # Without identifier, only UUID matching — UUID not in baseFqn or parentStepId
        steps = _extract_steps_for_stage("uuid-stage-xyz", node_map, "")
        assert len(steps) == 0

    def test_matches_by_parent_step_id(self) -> None:
        """Direct children (parentStepId == stage UUID) should still be found."""
        node_map = {
            "uuid-step-1": {
                "identifier": "run_sonarqube",
                "name": "run_sonarqube",
                "stepType": "IntegrationStageStepPMS",
                "status": "Failed",
                "baseFqn": "pipeline.stages.run_sonarqube",
                "parentStepId": "uuid-stage-abc",
            },
        }
        steps = _extract_steps_for_stage("uuid-stage-abc", node_map, "run_sonarqube")
        assert len(steps) == 1

    def test_does_not_match_wrong_stage_identifier(self) -> None:
        """Steps from a different stage should not be matched."""
        node_map = {
            "uuid-step-1": {
                "identifier": "build_image",
                "name": "build_image",
                "stepType": "Run",
                "status": "Success",
                "baseFqn": "pipeline.stages.build.spec.execution.steps.build_image",
                "parentStepId": "uuid-other",
            },
        }
        steps = _extract_steps_for_stage("uuid-stage-xyz", node_map, "run_sonarqube")
        assert len(steps) == 0
