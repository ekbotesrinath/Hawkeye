"""Terraform plan parser and release precheck tools.

2 tools:
- hawkeye_parse_terraform_plan
- hawkeye_release_precheck_report
"""

from __future__ import annotations

import asyncio
import re

from mcp.server.fastmcp import FastMCP

from core.formatters import format_status_icon
from core.models import ExecutionStatus, PipelineExecution, StageExecution
from providers.harness.url_patterns import parse_harness_url
from tools._helpers import get_provider, run_tool, status_icon

# Regex to extract Terraform plan summary line:
# "Plan: X to add, Y to change, Z to destroy."
_PLAN_RE = re.compile(
    r"Plan:\s*(\d+)\s+to add,\s*(\d+)\s+to change,\s*(\d+)\s+to destroy",
    re.IGNORECASE,
)

# "No changes. Infrastructure is up-to-date." or
# "No changes. Your infrastructure matches the configuration."
_NO_CHANGES_RE = re.compile(r"No changes\.", re.IGNORECASE)


def register_terraform_tools(mcp: FastMCP) -> None:
    """Register Terraform plan parser tools on the MCP server."""

    @mcp.tool()
    async def hawkeye_parse_terraform_plan(
        execution_id: str = "",
        url: str = "",
    ) -> str:
        """Parse Terraform plan output from a pipeline execution.

        Finds all stages that contain Terraform plan output, extracts the plan
        summary per layer (X to add, Y to change, Z to destroy), and returns a
        structured markdown table.

        Accepts either an execution_id or a Harness UI URL.
        """
        params = {"execution_id": execution_id, "url": url}

        async def _run() -> str:
            eid = execution_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
            if not eid:
                return "Error: Provide either execution_id or a Harness execution URL."

            provider = await get_provider()
            execution = await provider.get_execution(eid)
            layers = await _extract_terraform_layers(execution, provider)

            if not layers:
                return (
                    f"No Terraform plan output found in execution "
                    f"`{execution.pipeline_name}` (Run #{execution.run_sequence})."
                )

            return _format_plan_summary(execution, layers)

        return await run_tool("hawkeye_parse_terraform_plan", params, _run())

    @mcp.tool()
    async def hawkeye_release_precheck_report(
        infra_builder_execution_id: str,
        infra_createenv_execution_id: str,
        branch: str = "",
    ) -> str:
        """Generate a combined release precheck report from two pipeline executions.

        Fetches BOTH infra_builder and infra_createenv executions, parses
        Terraform plan output from each, and combines them into a unified
        release precheck report with Terraform changes and pipeline dry-run status.
        """
        params = {
            "infra_builder_execution_id": infra_builder_execution_id,
            "infra_createenv_execution_id": infra_createenv_execution_id,
            "branch": branch,
        }

        async def _run() -> str:
            provider = await get_provider()

            ex_builder, ex_createenv = await asyncio.gather(
                provider.get_execution(infra_builder_execution_id),
                provider.get_execution(infra_createenv_execution_id),
            )

            layers_builder, layers_createenv = await asyncio.gather(
                _extract_terraform_layers(ex_builder, provider),
                _extract_terraform_layers(ex_createenv, provider),
            )

            # Merge all layers
            all_layers: dict[str, TerraformLayer] = {}
            for layer in layers_builder + layers_createenv:
                all_layers[layer.name] = layer

            branch_label = branch or ex_builder.branch or "unknown"
            return _format_precheck_report(
                branch_label, ex_builder, ex_createenv, list(all_layers.values())
            )

        return await run_tool("hawkeye_release_precheck_report", params, _run())


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------


class TerraformLayer:
    """Parsed Terraform plan result for a single layer."""

    __slots__ = ("name", "to_add", "to_change", "to_destroy", "has_changes", "stage_status")

    def __init__(
        self,
        name: str,
        to_add: int = 0,
        to_change: int = 0,
        to_destroy: int = 0,
        *,
        has_changes: bool = False,
        stage_status: ExecutionStatus = ExecutionStatus.SUCCESS,
    ) -> None:
        self.name = name
        self.to_add = to_add
        self.to_change = to_change
        self.to_destroy = to_destroy
        self.has_changes = has_changes
        self.stage_status = stage_status


# ------------------------------------------------------------------
# Extraction
# ------------------------------------------------------------------


async def _extract_terraform_layers(
    execution: PipelineExecution,
    provider: object,
) -> list[TerraformLayer]:
    """Extract Terraform plan results from all stages in an execution."""
    layers: list[TerraformLayer] = []

    # Collect stages that look like Terraform plan layers
    plan_stages = _find_plan_stages(execution)

    if not plan_stages:
        return layers

    # Fetch logs in parallel for all steps that might contain plan output
    log_tasks = []
    stage_step_map: list[tuple[StageExecution, str, str]] = []

    for stage in plan_stages:
        for step in stage.steps:
            step_name_lower = step.name.lower()
            if any(
                kw in step_name_lower
                for kw in ("terraform", "tf_plan", "tfplan", "plan", "shell")
            ):
                log_tasks.append(
                    provider.get_step_logs(execution.plan_execution_id, stage.stage_id, step.step_id)
                )
                stage_step_map.append((stage, stage.stage_id, step.step_id))

    if not log_tasks:
        # Fallback: try first step of each plan stage
        for stage in plan_stages:
            if stage.steps:
                step = stage.steps[0]
                log_tasks.append(
                    provider.get_step_logs(
                        execution.plan_execution_id, stage.stage_id, step.step_id
                    )
                )
                stage_step_map.append((stage, stage.stage_id, step.step_id))

    if not log_tasks:
        return layers

    log_results = await asyncio.gather(*log_tasks, return_exceptions=True)

    # Parse each result for Terraform plan output
    seen_names: set[str] = set()
    for i, (stage, _sid, _stid) in enumerate(stage_step_map):
        raw = log_results[i]
        if isinstance(raw, BaseException):
            continue
        # get_step_logs now returns dict with 'node' and 'subgraph' keys
        log_text = ""
        if isinstance(raw, dict):
            from providers.harness.parser import extract_node_details
            node_data = raw.get("node")
            subgraph_data = raw.get("subgraph")
            if node_data:
                log_text = extract_node_details(node_data, subgraph_data)
        else:
            log_text = str(raw)
        layer = _parse_plan_from_logs(stage.name, log_text, stage.status)
        if layer and layer.name not in seen_names:
            layers.append(layer)
            seen_names.add(layer.name)

    # Add stages without logs as no-change or unknown
    for stage in plan_stages:
        layer_name = _normalize_layer_name(stage.name)
        if layer_name not in seen_names:
            layers.append(
                TerraformLayer(
                    name=layer_name, has_changes=False, stage_status=stage.status
                )
            )
            seen_names.add(layer_name)

    return layers


def _find_plan_stages(execution: PipelineExecution) -> list[StageExecution]:
    """Find stages that look like Terraform plan stages."""
    plan_stages: list[StageExecution] = []
    for stage in execution.stages:
        name_lower = stage.name.lower()
        if any(
            kw in name_lower
            for kw in ("layer", "terraform", "tf_plan", "infra", "plan")
        ):
            plan_stages.append(stage)
    return plan_stages


def _normalize_layer_name(stage_name: str) -> str:
    """Extract a clean layer name from a stage name.

    Examples:
        "TF_Plan_Layer_2" → "Layer 2"
        "terraform_plan_layer_3.5" → "Layer 3.5"
        "Layer 2.5 Plan" → "Layer 2.5"
        "ASB Layer 6" → "ASB Layer 6"
        "PostgreSQL" → "PostgreSQL"
        "SMTP Layer" → "SMTP Layer"
    """
    # Try to find "Layer X" or "Layer X.Y" pattern
    layer_match = re.search(r"((?:\w+\s+)?Layer\s+[\d.]+)", stage_name, re.IGNORECASE)
    if layer_match:
        result = layer_match.group(1).strip()
        # Ensure "Layer" is title-cased
        result = re.sub(r"(?i)\blayer\b", "Layer", result)
        return result

    # Try to remove common prefixes like "TF_Plan_", "terraform_plan_"
    cleaned = re.sub(r"^(tf_plan_|terraform_plan_|plan_)", "", stage_name, flags=re.IGNORECASE)
    cleaned = re.sub(r"[_]+", " ", cleaned).strip()

    # Re-check cleaned string for layer pattern
    layer_match = re.search(r"((?:\w+\s+)?Layer\s+[\d.]+)", cleaned, re.IGNORECASE)
    if layer_match:
        result = layer_match.group(1).strip()
        result = re.sub(r"(?i)\blayer\b", "Layer", result)
        return result

    return cleaned or stage_name


def _parse_plan_from_logs(
    stage_name: str, log_text: str, stage_status: ExecutionStatus
) -> TerraformLayer | None:
    """Parse Terraform plan summary from log text."""
    layer_name = _normalize_layer_name(stage_name)

    # Check for "No changes" first
    if _NO_CHANGES_RE.search(log_text):
        return TerraformLayer(
            name=layer_name, has_changes=False, stage_status=stage_status
        )

    # Look for plan summary
    match = _PLAN_RE.search(log_text)
    if match:
        to_add = int(match.group(1))
        to_change = int(match.group(2))
        to_destroy = int(match.group(3))
        has_changes = (to_add + to_change + to_destroy) > 0
        return TerraformLayer(
            name=layer_name,
            to_add=to_add,
            to_change=to_change,
            to_destroy=to_destroy,
            has_changes=has_changes,
            stage_status=stage_status,
        )

    return None


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------


def _format_plan_summary(
    execution: PipelineExecution,
    layers: list[TerraformLayer],
) -> str:
    """Format Terraform plan summary as markdown."""
    lines = [
        f"## Terraform Plan Summary: {execution.pipeline_name} "
        f"(Run #{execution.run_sequence}, {execution.branch or 'unknown'})",
        "",
        "| Layer | Changes | Detail |",
        "|-------|---------|--------|",
    ]

    for layer in layers:
        if layer.has_changes:
            detail = (
                f"{layer.to_add} to add, "
                f"{layer.to_change} to change, "
                f"{layer.to_destroy} to destroy"
            )
            lines.append(f"| {layer.name} | ⚠️ Changes | {detail} |")
        else:
            lines.append(f"| {layer.name} | ✅ No changes | — |")

    return "\n".join(lines)


def _format_precheck_report(
    branch: str,
    ex_builder: PipelineExecution,
    ex_createenv: PipelineExecution,
    layers: list[TerraformLayer],
) -> str:
    """Format the full release precheck report."""
    lines = [
        f"## Release Precheck Report: {branch}",
        "",
    ]

    # Terraform Changes section
    lines.append("### Terraform Changes")
    lines.append("| Layer | Changes | Detail |")
    lines.append("|-------|---------|--------|")

    changed = 0
    no_change = 0
    for layer in layers:
        if layer.has_changes:
            detail = (
                f"{layer.to_add} to add, "
                f"{layer.to_change} to change, "
                f"{layer.to_destroy} to destroy"
            )
            lines.append(f"| {layer.name} | ⚠️ Changes | {detail} |")
            changed += 1
        else:
            lines.append(f"| {layer.name} | ✅ No changes | — |")
            no_change += 1

    lines.append("")

    # Pipeline Dry Runs section
    lines.append("### Pipeline Dry Runs")
    lines.append("| Layer | Status | Pipeline | Build ID |")
    lines.append("|-------|--------|----------|----------|")

    all_stages = []
    for ex in (ex_builder, ex_createenv):
        for stage in ex.stages:
            icon = status_icon(stage.status)
            link = ex.execution_url
            link_text = (
                f"[{ex.pipeline_name} (#{ex.run_sequence})]({link})"
                if link
                else f"{ex.pipeline_name} (#{ex.run_sequence})"
            )
            layer_name = _normalize_layer_name(stage.name)
            lines.append(
                f"| {layer_name} | {icon} {stage.status.value} "
                f"| {link_text} | [Link]({link}) |"
            )
            all_stages.append(stage)

    lines.append("")

    # Summary section
    total = len(layers) if layers else len(all_stages)
    success_count = sum(
        1
        for s in all_stages
        if s.status == ExecutionStatus.SUCCESS
    )
    running_count = sum(
        1
        for s in all_stages
        if s.status == ExecutionStatus.RUNNING
    )
    other_count = len(all_stages) - success_count - running_count

    lines.append("### Summary")
    lines.append(f"- Total layers: {total}")
    lines.append(f"- Layers with changes: {changed}")
    lines.append(f"- Layers without changes: {no_change}")

    status_parts = []
    if success_count:
        status_parts.append(f"{success_count}/{len(all_stages)} Success")
    if running_count:
        status_parts.append(f"{running_count}/{len(all_stages)} Running")
    if other_count:
        status_parts.append(f"{other_count}/{len(all_stages)} Other")

    lines.append(f"- All dry runs: {', '.join(status_parts)}")

    return "\n".join(lines)
