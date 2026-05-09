"""Tests for providers.harness.url_patterns — Harness URL parser."""

from __future__ import annotations

import pytest

from providers.harness.url_patterns import ParsedHarnessUrl, parse_harness_url


class TestStandardExecutionUrl:
    def test_ci_module_execution(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/myproj/pipelines/Build_Deploy"
            "/executions/exec_001/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.base_url == "https://app.harness.io"
        assert parsed.account_id == "abc123"
        assert parsed.module == "ci"
        assert parsed.org_identifier == "default"
        assert parsed.project_identifier == "myproj"
        assert parsed.pipeline_identifier == "Build_Deploy"
        assert parsed.execution_id == "exec_001"

    def test_cd_module_execution(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/cd"
            "/orgs/myorg/projects/myproj/pipelines/Deploy_Prod"
            "/executions/exec_002/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.module == "cd"
        assert parsed.pipeline_identifier == "Deploy_Prod"
        assert parsed.execution_id == "exec_002"


class TestUrlWithStageSelected:
    def test_stage_query_param(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline?stage=Build_Stage"
        )
        parsed = parse_harness_url(url)
        assert parsed.stage_id == "Build_Stage"
        assert parsed.step_id is None

    def test_stage_and_step(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline?stage=Build&step=Compile"
        )
        parsed = parse_harness_url(url)
        assert parsed.stage_id == "Build"
        assert parsed.step_id == "Compile"


class TestUrlWithConnectorAndBranch:
    def test_full_remote_pipeline_url(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline"
            "?connectorRef=account.GHConnector"
            "&repoName=dfin-harness-pipelines"
            "&branch=release_26_2"
            "&storeType=REMOTE"
            "&stage=Build_Stage"
            "&step=Compile_Step"
            "&childStage=child1"
            "&stageExecId=stageExec1"
        )
        parsed = parse_harness_url(url)
        assert parsed.connector_ref == "account.GHConnector"
        assert parsed.repo_name == "dfin-harness-pipelines"
        assert parsed.branch == "release_26_2"
        assert parsed.store_type == "REMOTE"
        assert parsed.child_stage_id == "child1"
        assert parsed.stage_exec_id == "stageExec1"
        assert parsed.stage_id == "Build_Stage"
        assert parsed.step_id == "Compile_Step"


class TestVanityUrl:
    def test_custom_domain(self) -> None:
        url = (
            "https://harness.company.com/ng/account/xyz789/module/cd"
            "/orgs/prodorg/projects/prodproj/pipelines/Deploy"
            "/executions/exec_100/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.base_url == "https://harness.company.com"
        assert parsed.account_id == "xyz789"
        assert parsed.org_identifier == "prodorg"

    def test_vanity_with_port(self) -> None:
        url = (
            "https://harness.local:8443/ng/account/acc1/module/ci"
            "/orgs/org1/projects/proj1/pipelines/pipe1"
            "/executions/exec1/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.base_url == "https://harness.local:8443"


class TestPipelineStudioUrl:
    def test_pipeline_studio_no_execution(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/pipeline-studio"
        )
        parsed = parse_harness_url(url)
        assert parsed.pipeline_identifier == "pipe1"
        assert parsed.execution_id is None
        assert parsed.stage_id is None


class TestNoModuleInUrl:
    def test_url_without_module(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.module is None
        assert parsed.pipeline_identifier == "pipe1"
        assert parsed.execution_id == "exec1"


class TestInvalidUrls:
    def test_non_harness_url(self) -> None:
        with pytest.raises(ValueError, match="Not a recognised Harness URL"):
            parse_harness_url("https://jenkins.io/job/build/123")

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError):
            parse_harness_url("")

    def test_random_harness_page(self) -> None:
        with pytest.raises(ValueError):
            parse_harness_url("https://app.harness.io/ng/account/abc123/dashboard")

    def test_missing_pipeline_segment(self) -> None:
        with pytest.raises(ValueError):
            parse_harness_url(
                "https://app.harness.io/ng/account/abc123/module/ci"
                "/orgs/default/projects/proj"
            )


class TestExtraQueryParams:
    def test_unknown_query_params_ignored(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline?foo=bar&baz=qux"
        )
        parsed = parse_harness_url(url)
        assert parsed.execution_id == "exec1"
        # Unknown params don't cause errors
        assert parsed.stage_id is None


class TestParsedHarnessUrlDataclass:
    def test_frozen(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline"
        )
        parsed = parse_harness_url(url)
        with pytest.raises(AttributeError):
            parsed.account_id = "new_id"  # type: ignore[misc]

    def test_defaults(self) -> None:
        p = ParsedHarnessUrl(base_url="https://x.io", account_id="a")
        assert p.module is None
        assert p.execution_id is None
        assert p.stage_id is None
        assert p.branch is None

    def test_equality(self) -> None:
        p1 = ParsedHarnessUrl(base_url="https://x.io", account_id="a")
        p2 = ParsedHarnessUrl(base_url="https://x.io", account_id="a")
        assert p1 == p2


class TestExecutionUrlWithoutTrailingPipeline:
    def test_execution_url_without_pipeline_suffix(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/ci"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1"
        )
        parsed = parse_harness_url(url)
        assert parsed.execution_id == "exec1"


class TestDeploymentsUrl:
    def test_cd_deployments_url(self) -> None:
        url = (
            "https://app.harness.io/ng/account/QbXF1VIZTIC2/module/ci"
            "/orgs/default/projects/eastwood/pipelines/ci_cd_AD"
            "/deployments/3zW4fnj5TT-e0MNcA9Io3g/pipeline"
            "?connectorRef=account.GHConnector"
            "&repoName=dfin-harness-pipelines&storeType=REMOTE"
        )
        parsed = parse_harness_url(url)
        assert parsed.account_id == "QbXF1VIZTIC2"
        assert parsed.module == "ci"
        assert parsed.org_identifier == "default"
        assert parsed.project_identifier == "eastwood"
        assert parsed.pipeline_identifier == "ci_cd_AD"
        assert parsed.execution_id == "3zW4fnj5TT-e0MNcA9Io3g"
        assert parsed.connector_ref == "account.GHConnector"
        assert parsed.repo_name == "dfin-harness-pipelines"
        assert parsed.store_type == "REMOTE"

    def test_deployments_url_without_trailing_pipeline(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/module/cd"
            "/orgs/myorg/projects/myproj/pipelines/Deploy_Prod"
            "/deployments/deploy_exec_001"
        )
        parsed = parse_harness_url(url)
        assert parsed.pipeline_identifier == "Deploy_Prod"
        assert parsed.execution_id == "deploy_exec_001"


class TestAllViewUrl:
    def test_all_view_execution_url(self) -> None:
        url = (
            "https://app.harness.io/ng/account/QbXF1VIZTIC2-ueN1uTWHQ/all"
            "/orgs/default/projects/eastwood/pipelines/cd_deploy_env"
            "/deployments/UqljtMGrS7qDqoiKQzsWvg/pipeline"
            "?connectorRef=account.GHConnector"
            "&repoName=dfin-harness-pipelines&storeType=REMOTE"
        )
        parsed = parse_harness_url(url)
        assert parsed.account_id == "QbXF1VIZTIC2-ueN1uTWHQ"
        assert parsed.module is None
        assert parsed.org_identifier == "default"
        assert parsed.project_identifier == "eastwood"
        assert parsed.pipeline_identifier == "cd_deploy_env"
        assert parsed.execution_id == "UqljtMGrS7qDqoiKQzsWvg"
        assert parsed.connector_ref == "account.GHConnector"
        assert parsed.repo_name == "dfin-harness-pipelines"
        assert parsed.store_type == "REMOTE"

    def test_all_view_with_executions_path(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/all"
            "/orgs/myorg/projects/myproj/pipelines/Build_Deploy"
            "/executions/exec_001/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.module is None
        assert parsed.execution_id == "exec_001"

    def test_all_view_pipeline_studio(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/all"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/pipeline-studio"
        )
        parsed = parse_harness_url(url)
        assert parsed.module is None
        assert parsed.pipeline_identifier == "pipe1"
        assert parsed.execution_id is None

    def test_all_view_with_stage_and_step_params(self) -> None:
        url = (
            "https://app.harness.io/ng/account/abc123/all"
            "/orgs/default/projects/proj/pipelines/pipe1"
            "/executions/exec1/pipeline?stage=StageX&step=StepY"
        )
        parsed = parse_harness_url(url)
        assert parsed.module is None
        assert parsed.stage_id == "StageX"
        assert parsed.step_id == "StepY"

    def test_all_view_vanity_domain(self) -> None:
        url = (
            "https://harness.company.com/ng/account/xyz789/all"
            "/orgs/prodorg/projects/prodproj/pipelines/Deploy"
            "/executions/exec_100/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.base_url == "https://harness.company.com"
        assert parsed.module is None
        assert parsed.account_id == "xyz789"


class TestComplexPipelineIdentifiers:
    def test_pipeline_with_underscores(self) -> None:
        url = (
            "https://app.harness.io/ng/account/acc1/module/ci"
            "/orgs/org/projects/proj/pipelines/My_Complex_Pipeline_Name"
            "/executions/exec1/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.pipeline_identifier == "My_Complex_Pipeline_Name"

    def test_pipeline_with_hyphens(self) -> None:
        url = (
            "https://app.harness.io/ng/account/acc1/module/ci"
            "/orgs/org/projects/proj/pipelines/my-pipeline-name"
            "/executions/exec1/pipeline"
        )
        parsed = parse_harness_url(url)
        assert parsed.pipeline_identifier == "my-pipeline-name"
