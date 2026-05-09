"""Tests for tools/terraform.py — Terraform plan parser and release precheck."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.models import (
    ExecutionStatus,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from tools.terraform import (
    TerraformLayer,
    _find_plan_stages,
    _format_plan_summary,
    _format_precheck_report,
    _normalize_layer_name,
    _parse_plan_from_logs,
)

_NOW_MS = int(time.time() * 1000)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_step(
    step_id: str = "step1",
    name: str = "Terraform Plan",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
) -> StepExecution:
    return StepExecution(
        step_id=step_id,
        name=name,
        identifier=step_id,
        status=status,
        step_type="ShellScript",
        start_ts=_NOW_MS - 60_000,
        end_ts=_NOW_MS,
    )


def _make_stage(
    stage_id: str = "stage1",
    name: str = "TF_Plan_Layer_2",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    steps: list[StepExecution] | None = None,
) -> StageExecution:
    return StageExecution(
        stage_id=stage_id,
        name=name,
        identifier=stage_id,
        status=status,
        start_ts=_NOW_MS - 120_000,
        end_ts=_NOW_MS,
        stage_type="Custom",
        steps=steps or [_make_step()],
    )


def _make_execution(
    execution_id: str = "exec1",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    stages: list[StageExecution] | None = None,
    pipeline_name: str = "infra_builder",
    run_sequence: int = 2065,
    branch: str | None = "release_26_2",
) -> PipelineExecution:
    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier="infra_builder",
        pipeline_name=pipeline_name,
        status=status,
        start_ts=_NOW_MS - 600_000,
        end_ts=_NOW_MS,
        trigger_type="MANUAL",
        triggered_by="user@test.com",
        branch=branch,
        run_sequence=run_sequence,
        execution_url="https://app.harness.io/exec/1",
        stages=stages or [],
    )


# ------------------------------------------------------------------
# _normalize_layer_name
# ------------------------------------------------------------------


class TestNormalizeLayerName:
    def test_tf_plan_prefix(self) -> None:
        assert _normalize_layer_name("TF_Plan_Layer_2") == "Layer 2"

    def test_terraform_plan_prefix(self) -> None:
        assert _normalize_layer_name("terraform_plan_layer_3.5") == "Layer 3.5"

    def test_layer_with_suffix(self) -> None:
        assert _normalize_layer_name("Layer 2.5 Plan") == "Layer 2.5"

    def test_named_layer(self) -> None:
        assert _normalize_layer_name("ASB Layer 6") == "ASB Layer 6"

    def test_non_layer_name(self) -> None:
        result = _normalize_layer_name("PostgreSQL")
        assert result == "PostgreSQL"

    def test_smtp_layer(self) -> None:
        result = _normalize_layer_name("SMTP Layer")
        assert "SMTP" in result


# ------------------------------------------------------------------
# _parse_plan_from_logs
# ------------------------------------------------------------------


class TestParsePlanFromLogs:
    def test_standard_plan_output(self) -> None:
        log = (
            "Terraform plan output:\n"
            "Plan: 1 to add, 5 to change, 1 to destroy.\n"
            "Done."
        )
        layer = _parse_plan_from_logs("Layer 2", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.has_changes is True
        assert layer.to_add == 1
        assert layer.to_change == 5
        assert layer.to_destroy == 1

    def test_no_changes(self) -> None:
        log = (
            "Terraform output:\n"
            "No changes. Infrastructure is up-to-date.\n"
        )
        layer = _parse_plan_from_logs("Layer 3", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.has_changes is False
        assert layer.to_add == 0

    def test_no_changes_alternative_message(self) -> None:
        log = "No changes. Your infrastructure matches the configuration."
        layer = _parse_plan_from_logs("Layer 4", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.has_changes is False

    def test_destroy_only(self) -> None:
        log = "Plan: 0 to add, 0 to change, 3 to destroy."
        layer = _parse_plan_from_logs("Layer 5", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.has_changes is True
        assert layer.to_add == 0
        assert layer.to_change == 0
        assert layer.to_destroy == 3

    def test_add_only(self) -> None:
        log = "Plan: 4 to add, 0 to change, 0 to destroy."
        layer = _parse_plan_from_logs("ASB Layer 6", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.has_changes is True
        assert layer.to_add == 4

    def test_zero_changes(self) -> None:
        log = "Plan: 0 to add, 0 to change, 0 to destroy."
        layer = _parse_plan_from_logs("Layer 7", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.has_changes is False

    def test_no_terraform_output(self) -> None:
        log = "Running build step...\nBuild succeeded."
        layer = _parse_plan_from_logs("Layer 8", log, ExecutionStatus.SUCCESS)
        assert layer is None

    def test_case_insensitive_plan(self) -> None:
        log = "plan: 2 to add, 1 to change, 0 to destroy."
        layer = _parse_plan_from_logs("Layer 9", log, ExecutionStatus.SUCCESS)
        assert layer is not None
        assert layer.to_add == 2

    def test_stage_status_preserved(self) -> None:
        log = "Plan: 1 to add, 0 to change, 0 to destroy."
        layer = _parse_plan_from_logs("Layer 2", log, ExecutionStatus.RUNNING)
        assert layer is not None
        assert layer.stage_status == ExecutionStatus.RUNNING


# ------------------------------------------------------------------
# _find_plan_stages
# ------------------------------------------------------------------


class TestFindPlanStages:
    def test_finds_layer_stages(self) -> None:
        stages = [
            _make_stage(name="TF_Plan_Layer_2"),
            _make_stage(name="Build", stage_id="s2"),
            _make_stage(name="Layer 3 Plan", stage_id="s3"),
        ]
        execution = _make_execution(stages=stages)
        found = _find_plan_stages(execution)
        assert len(found) == 2
        assert found[0].name == "TF_Plan_Layer_2"
        assert found[1].name == "Layer 3 Plan"

    def test_finds_terraform_stages(self) -> None:
        stages = [_make_stage(name="Terraform Apply")]
        execution = _make_execution(stages=stages)
        found = _find_plan_stages(execution)
        assert len(found) == 1

    def test_finds_infra_stages(self) -> None:
        stages = [_make_stage(name="infra_setup")]
        execution = _make_execution(stages=stages)
        found = _find_plan_stages(execution)
        assert len(found) == 1

    def test_no_plan_stages(self) -> None:
        stages = [_make_stage(name="Build"), _make_stage(name="Deploy", stage_id="s2")]
        execution = _make_execution(stages=stages)
        found = _find_plan_stages(execution)
        assert len(found) == 0


# ------------------------------------------------------------------
# _format_plan_summary
# ------------------------------------------------------------------


class TestFormatPlanSummary:
    def test_basic_format(self) -> None:
        layers = [
            TerraformLayer("Layer 2", 1, 5, 1, has_changes=True),
            TerraformLayer("Layer 2.5", 0, 0, 0, has_changes=False),
            TerraformLayer("Layer 3", 1, 0, 1, has_changes=True),
        ]
        execution = _make_execution()
        result = _format_plan_summary(execution, layers)
        assert "## Terraform Plan Summary" in result
        assert "infra_builder" in result
        assert "| Layer 2 | ⚠️ Changes |" in result
        assert "| Layer 2.5 | ✅ No changes |" in result
        assert "1 to add, 5 to change, 1 to destroy" in result

    def test_all_no_changes(self) -> None:
        layers = [
            TerraformLayer("Layer 2", has_changes=False),
            TerraformLayer("Layer 3", has_changes=False),
        ]
        execution = _make_execution()
        result = _format_plan_summary(execution, layers)
        assert "⚠️" not in result
        assert "✅ No changes" in result


# ------------------------------------------------------------------
# _format_precheck_report
# ------------------------------------------------------------------


class TestFormatPrecheckReport:
    def test_basic_report(self) -> None:
        layers = [
            TerraformLayer("Layer 2", 1, 5, 1, has_changes=True),
            TerraformLayer("Layer 2.5", has_changes=False),
        ]
        ex_builder = _make_execution(
            stages=[
                _make_stage(name="Layer 2", status=ExecutionStatus.SUCCESS),
                _make_stage(name="Layer 2.5", status=ExecutionStatus.SUCCESS, stage_id="s2"),
            ],
        )
        ex_createenv = _make_execution(
            execution_id="exec2",
            pipeline_name="infra_createenv",
            stages=[
                _make_stage(name="Layer 3", status=ExecutionStatus.RUNNING, stage_id="s3"),
            ],
        )
        result = _format_precheck_report("release_26_2", ex_builder, ex_createenv, layers)
        assert "## Release Precheck Report: release_26_2" in result
        assert "### Terraform Changes" in result
        assert "### Pipeline Dry Runs" in result
        assert "### Summary" in result
        assert "Layers with changes: 1" in result
        assert "Layers without changes: 1" in result

    def test_with_running_stages(self) -> None:
        layers = [TerraformLayer("Layer 2", 1, 0, 0, has_changes=True)]
        ex_builder = _make_execution(
            stages=[_make_stage(name="Layer 2", status=ExecutionStatus.RUNNING)],
        )
        ex_createenv = _make_execution(
            execution_id="exec2",
            stages=[],
        )
        result = _format_precheck_report("release_26_2", ex_builder, ex_createenv, layers)
        assert "🔄" in result

    def test_with_aborted_stages(self) -> None:
        layers = [TerraformLayer("Layer 2", has_changes=False)]
        ex_builder = _make_execution(
            stages=[_make_stage(name="Layer 2", status=ExecutionStatus.ABORTED)],
        )
        ex_createenv = _make_execution(execution_id="exec2", stages=[])
        result = _format_precheck_report("release_26_2", ex_builder, ex_createenv, layers)
        assert "🚫" in result

    def test_empty_layers(self) -> None:
        ex_builder = _make_execution(stages=[_make_stage(name="Layer 2")])
        ex_createenv = _make_execution(execution_id="exec2", stages=[])
        result = _format_precheck_report("release_26_2", ex_builder, ex_createenv, [])
        assert "Total layers:" in result
