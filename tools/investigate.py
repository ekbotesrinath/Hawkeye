"""Investigation tools — the flagship intelligence tools.

6 tools:
- hawkeye_diagnose (unified diagnosis — summary or full mode with optional console logs)
- hawkeye_investigate_failure (parallel multi-signal failure triage)
- hawkeye_compare_executions (diff two pipeline runs)
- hawkeye_get_failure_pattern (recurring failure detection)
- hawkeye_watch_execution (poll a running execution until it completes, then diagnose)
- hawkeye_get_flaky_steps (step-level flakiness analysis across recent runs)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from mcp.server.fastmcp import FastMCP

from core.models import (
    FAILED_STATUSES,
    SKIPPED_STATUSES,
    TERMINAL_STATUSES,
    ExecutionStatus,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from providers.harness.url_patterns import parse_harness_url
from tools._gather import (
    collect_failed_steps,
    enrich_ci_infra,
    find_child_execution_id,
    follow_child_pipelines,
    gather_investigation,
    is_pipeline_step_type,
)
from tools._helpers import (
    READ_ONLY_ANNOTATIONS,
    clamp_limit,
    format_duration_ms,
    format_relative_time,
    get_provider,
    resolve_pipeline_id,
    run_tool,
    status_icon,
)

logger = logging.getLogger(__name__)

# Backwards-compatible aliases — implementations live in tools/_gather.py
_FAILED_STATUSES = FAILED_STATUSES
_collect_failed_steps = collect_failed_steps
_follow_child_pipelines = follow_child_pipelines
_find_child_execution_id = find_child_execution_id
_is_pipeline_step_type = is_pipeline_step_type


def _enrich_from_raw_graph(
    execution: PipelineExecution,
    failed_steps: list[tuple[str, str, StepExecution]],
    step_details: dict[str, str],
    raw_node_map: dict,
) -> None:
    """Compatibility wrapper over :func:`tools._gather.enrich_ci_infra`."""
    covered = {stage_id for stage_id, _step_id, _step in failed_steps}
    enrich_ci_infra(execution, covered, step_details, raw_node_map)

_MAX_INVESTIGATION_LOG_LINES = 50

# Stage types that indicate CI module (child pipelines should use /module/ci/)
_CI_STAGE_TYPES = {"IntegrationStageStepPMS", "CI", "IntegrationStage"}


def _detect_module_for_stage(stage: StageExecution, raw_node_map: dict | None) -> str:
    """Detect whether a Pipeline-type stage's child is CI or CD.

    Checks:
    1. The parent stage's stage_type for CI indicators.
    2. The raw node map for nodeType containing 'Integration' or 'CI'.
    3. Defaults to 'ci' for Pipeline-type stages (most child pipelines are CI builds).
    """
    # Check stage_type directly
    if stage.stage_type in _CI_STAGE_TYPES:
        return "ci"
    if stage.stage_type and (
        "integration" in stage.stage_type.lower() or stage.stage_type.lower() == "ci"
    ):
        return "ci"

    # Check raw node map for CI indicators
    if raw_node_map and isinstance(raw_node_map, dict):
        for _uuid, raw_node in raw_node_map.items():
            if not isinstance(raw_node, dict):
                continue
            base_fqn = raw_node.get("baseFqn", "")
            node_name = raw_node.get("name", "")
            node_identifier = raw_node.get("identifier", "")
            if not (
                stage.identifier in base_fqn
                or node_name == stage.name
                or node_identifier == stage.identifier
            ):
                continue
            node_type = raw_node.get("nodeType", "")
            step_type = raw_node.get("stepType", "")
            if node_type in _CI_STAGE_TYPES or step_type in _CI_STAGE_TYPES:
                return "ci"
            if "integration" in node_type.lower() or "integration" in step_type.lower():
                return "ci"

    # Default to ci for child pipelines (most common in ci_cd_AD)
    return "ci"


def register_investigation_tools(mcp: FastMCP) -> None:
    """Register all investigation tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_diagnose(
        execution_id: str = "",
        url: str = "",
        mode: str = "full",
        include_logs: bool = True,
    ) -> str:
        """Unified diagnosis tool — single-call failure investigation.

        Replaces the multi-call pattern of investigate_failure + get_step_logs + get_all_stages.
        Accepts either an execution_id or a Harness UI URL (preferred — just paste the URL).

        Modes:
        - mode="summary": Execution overview, failed stage/step identification, failure messages,
          pipeline history, and quick analysis. Fast and lightweight.
        - mode="full": Everything from summary PLUS full step details for all failed steps,
          child pipeline following, rollback detection, CI infra enrichment,
          analysis, and deep links.

        Console logs (only applies when mode="full"):
        - include_logs=True (default): Downloads and appends console logs for each failed step.
        - include_logs=False: Shows step details from graph data without console output.
        """
        params = {
            "execution_id": execution_id,
            "url": url,
            "mode": mode,
            "include_logs": include_logs,
        }

        async def _run() -> str:
            if mode not in ("summary", "full"):
                return f"Invalid mode '{mode}'. Use 'summary' or 'full'."

            eid = execution_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
            if not eid:
                return "Error: Provide either execution_id or a Harness execution URL."

            provider = await get_provider()

            # Phase 1: Get execution detail
            execution = await provider.get_execution(eid)

            if mode == "summary":
                # Summary mode: execution + history + analysis only
                history_result: list[PipelineExecution] = []
                with contextlib.suppress(Exception):
                    history_result = await provider.list_recent_executions(
                        execution.pipeline_identifier, limit=5
                    )

                # Fetch execution inputs (best-effort)
                input_yaml = None
                with contextlib.suppress(Exception):
                    input_yaml = await provider.get_execution_inputs(eid)

                failed_steps = _collect_failed_steps(execution)
                analysis = _build_analysis(execution, failed_steps, history_result)
                return _format_summary(
                    execution, failed_steps, history_result, analysis, input_yaml,
                )

            # Full mode: shared investigation pipeline
            failed_steps = _collect_failed_steps(execution)
            history, _raw_node_map, step_details, child_findings = (
                await gather_investigation(
                    provider, eid, execution, include_logs=include_logs
                )
            )
            return _format_investigation(
                execution, failed_steps, step_details, history, child_findings
            )

        return await run_tool("hawkeye_diagnose", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_investigate_failure(
        execution_id: str = "",
        url: str = "",
    ) -> str:
        """THE flagship investigation tool — deep failure triage with parallel data gathering.

        Fires MULTIPLE API calls in parallel: full execution detail, execution graph,
        step logs for ALL failed steps, and last 5 executions of the same pipeline.
        Synthesizes everything into a unified failure investigation report with
        root cause, logs, pipeline history, analysis, and deep links.

        Accepts either an execution_id or a Harness UI URL (preferred — just paste the URL).
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

            # Phase 1: Get execution detail (needed to find pipeline_id and failed steps)
            execution = await provider.get_execution(eid)

            # Phases 2-4: shared investigation pipeline (history, step
            # details, CI infra enrichment, child pipeline following)
            failed_steps = _collect_failed_steps(execution)
            history, _raw_node_map, step_details, child_findings = (
                await gather_investigation(provider, eid, execution)
            )

            return _format_investigation(
                execution, failed_steps, step_details, history, child_findings
            )

        return await run_tool("hawkeye_investigate_failure", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_compare_executions(
        execution_id_1: str,
        execution_id_2: str,
    ) -> str:
        """Compare two pipeline executions and highlight differences.

        Shows input variable changes, branch differences, trigger info,
        stage timing differences, status differences, and skipped stages.
        Use case: "Compare the last success with this failure — what changed?"
        """
        params = {"execution_id_1": execution_id_1, "execution_id_2": execution_id_2}

        async def _run() -> str:
            provider = await get_provider()
            ex1, ex2 = await asyncio.gather(
                provider.get_execution(execution_id_1),
                provider.get_execution(execution_id_2),
            )
            return _format_comparison(ex1, ex2)

        return await run_tool("hawkeye_compare_executions", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_failure_pattern(
        pipeline_id: str,
        limit: int = 20,
    ) -> str:
        """Analyze recent executions for recurring failure patterns.

        Identifies which stages fail most often, whether failures are branch-specific,
        success rate over time, and trend direction.
        """
        params = {"pipeline_id": pipeline_id, "limit": limit}

        async def _run() -> str:
            safe_limit = clamp_limit(limit, hi=50, default=20)
            resolved_id = await resolve_pipeline_id(pipeline_id)
            provider = await get_provider()
            executions = await provider.list_recent_executions(resolved_id, limit=safe_limit)
            if not executions:
                return f"No executions found for pipeline `{resolved_id}`."
            return _format_failure_pattern(pipeline_id, executions)

        return await run_tool("hawkeye_get_failure_pattern", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_watch_execution(
        execution_id: str = "",
        url: str = "",
        poll_seconds: int = 30,
        timeout_minutes: int = 15,
    ) -> str:
        """Watch a running execution until it completes, then report.

        Polls the execution every *poll_seconds* (10-300, default 30) until
        it reaches a terminal status or *timeout_minutes* elapses (1-120,
        default 15). On success returns a completion summary; on failure
        returns the full Hawkeye failure investigation automatically.
        Ideal for "watch this deploy and tell me if it breaks".

        Accepts either an execution_id or a Harness UI URL.
        """
        params = {
            "execution_id": execution_id,
            "url": url,
            "poll_seconds": poll_seconds,
            "timeout_minutes": timeout_minutes,
        }

        async def _run() -> str:
            eid = execution_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
            if not eid:
                return "Error: Provide either execution_id or a Harness execution URL."

            interval = clamp_limit(poll_seconds, lo=10, hi=300, default=30)
            timeout_min = clamp_limit(timeout_minutes, lo=1, hi=120, default=15)
            deadline = time.monotonic() + timeout_min * 60

            provider = await get_provider()
            started = time.monotonic()
            polls = 0
            execution = await provider.get_execution(eid)

            while (
                execution.status not in TERMINAL_STATUSES
                and time.monotonic() + interval <= deadline
            ):
                await asyncio.sleep(interval)
                polls += 1
                execution = await provider.get_execution(eid)

            watched = _format_watch_duration(time.monotonic() - started)

            if execution.status not in TERMINAL_STATUSES:
                return _format_watch_timeout(execution, watched, timeout_min, polls)

            if execution.status == ExecutionStatus.SUCCESS:
                return _format_watch_success(execution, watched, polls)

            # Terminal but not successful — run the full investigation
            failed_steps = _collect_failed_steps(execution)
            history, _raw_node_map, step_details, child_findings = (
                await gather_investigation(provider, eid, execution)
            )
            report = _format_investigation(
                execution, failed_steps, step_details, history, child_findings
            )
            header = (
                f"## ⏱️ Watch complete: {execution.status.value.upper()} "
                f"after {watched} ({polls} poll(s))\n\n"
            )
            return header + report

        return await run_tool("hawkeye_watch_execution", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_flaky_steps(
        pipeline_id: str,
        limit: int = 15,
    ) -> str:
        """Detect flaky steps — steps that fail intermittently across runs.

        Fetches full step-level detail for the last *limit* runs (max 20)
        of a pipeline and computes per-step failure rates. Distinguishes
        FLAKY steps (fail sometimes — e.g. "this test step fails 30% of
        the time regardless of branch") from CONSISTENTLY BROKEN steps
        (fail every time they run), and notes whether failures are
        branch-specific.
        """
        params = {"pipeline_id": pipeline_id, "limit": limit}

        async def _run() -> str:
            safe_limit = clamp_limit(limit, hi=20, default=15)
            resolved_id = await resolve_pipeline_id(pipeline_id)
            provider = await get_provider()
            runs = await provider.list_recent_executions(resolved_id, limit=safe_limit)
            if not runs:
                return f"No executions found for pipeline `{resolved_id}`."

            # Summary listings have no step detail — fetch full executions
            # in parallel (client concurrency cap + terminal-status cache
            # keep this cheap on repeat calls).
            detail_results = await asyncio.gather(
                *[provider.get_execution(r.plan_execution_id) for r in runs],
                return_exceptions=True,
            )
            executions = [
                d for d in detail_results if isinstance(d, PipelineExecution)
            ]
            if not executions:
                return (
                    f"Could not fetch step-level detail for any of the "
                    f"{len(runs)} recent runs of `{resolved_id}`."
                )

            return _format_flaky_steps(resolved_id, executions)

        return await run_tool("hawkeye_get_flaky_steps", params, _run())


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _collect_rollback_steps(
    execution: PipelineExecution,
) -> list[tuple[str, str, StepExecution]]:
    """Return ``(stage_id, step_id, step)`` for every rollback step in failed stages."""
    rollbacks: list[tuple[str, str, StepExecution]] = []
    for stage in execution.stages:
        if stage.status not in _FAILED_STATUSES:
            continue
        for step in stage.steps:
            step_type_lower = (step.step_type or "").lower()
            name_lower = (step.name or "").lower()
            if "rollback" in step_type_lower or "rollback" in name_lower:
                rollbacks.append((stage.stage_id, step.step_id, step))
    return rollbacks


# Child pipeline following, CI infra enrichment, and step detail assembly
# live in tools/_gather.py — see the compatibility aliases at the top of
# this module.


def _truncate_logs(raw: str, max_lines: int = _MAX_INVESTIGATION_LOG_LINES) -> str:
    """Return last *max_lines* of log text with truncation note."""
    from core.formatters import truncate_logs

    return truncate_logs(raw, max_lines)


# ------------------------------------------------------------------
# Summary report formatter (for hawkeye_diagnose mode="summary")
# ------------------------------------------------------------------


def _format_summary(
    execution: PipelineExecution,
    failed_steps: list[tuple[str, str, StepExecution]],
    history: list[PipelineExecution] | BaseException,
    analysis: list[str],
    input_yaml: str | None = None,
) -> str:
    """Build a lightweight summary diagnosis report."""
    duration = format_duration_ms(execution.start_ts, execution.end_ts)
    branch_info = execution.branch or "—"
    trigger = (
        f"{execution.trigger_type} by {execution.triggered_by}"
        if execution.triggered_by
        else execution.trigger_type
    )
    icon = status_icon(execution.status)

    lines: list[str] = []

    # Header
    lines.append(
        f"## Hawkeye Diagnosis (Summary): "
        f"{execution.pipeline_name} (Run #{execution.run_sequence})"
    )
    lines.append(
        f"Status: {icon} {execution.status.value.upper()} | "
        f"Duration: {duration} | Branch: {branch_info} | Trigger: {trigger}"
    )
    lines.append("")

    # Execution Inputs
    if input_yaml and input_yaml.strip():
        lines.append("### Given Inputs")
        lines.append(f"```yaml\n{input_yaml.strip()}\n```")
        lines.append("")

    # Failed At
    if failed_steps:
        lines.append("### Failed At")
        for stage_id, _step_id, step in failed_steps:
            stage_name = stage_id
            for s in execution.stages:
                if s.stage_id == stage_id:
                    stage_name = s.name
                    break
            lines.append(
                f"Stage: {stage_name} → Step: {step.name} ({step.step_type})"
            )
            if step.failure_info and step.failure_info.message:
                lines.append(f'Error: "{step.failure_info.message}"')
        lines.append("")

    # Pipeline History
    if isinstance(history, list) and history:
        lines.append(f"### Pipeline History (last {len(history)} runs)")
        lines.append("| # | Status | Branch | When |")
        lines.append("|---|--------|--------|------|")

        for hx in history:
            hx_icon = status_icon(hx.status)
            hx_branch = hx.branch or "—"
            when = format_relative_time(hx.start_ts)
            lines.append(
                f"| {hx.run_sequence} | {hx_icon} {hx.status.value}"
                f" | {hx_branch} | {when} |"
            )
        lines.append("")

    # Quick Analysis
    lines.append("### Quick Analysis")
    for item in analysis:
        lines.append(f"- {item}")
    lines.append("")

    # Footer
    if execution.execution_url:
        lines.append(f"Harness: {execution.execution_url}")
    lines.append('💡 Run with mode="full" for step details and console logs.')

    return "\n".join(lines)


# ------------------------------------------------------------------
# Investigation report formatter
# ------------------------------------------------------------------


def _format_investigation(
    execution: PipelineExecution,
    failed_steps: list[tuple[str, str, StepExecution]],
    step_logs: dict[str, str],
    history: list[PipelineExecution] | BaseException,
    child_findings: dict[str, dict] | None = None,
) -> str:
    """Build the unified failure investigation report."""
    duration = format_duration_ms(execution.start_ts, execution.end_ts)
    branch_info = execution.branch or "—"
    trigger = (
        f"{execution.trigger_type} by {execution.triggered_by}"
        if execution.triggered_by
        else execution.trigger_type
    )

    lines: list[str] = []

    # Header
    lines.append(
        f"## 🔍 Hawkeye Failure Investigation: "
        f"{execution.pipeline_name} (Run #{execution.run_sequence})"
    )
    lines.append("")

    # Summary
    if failed_steps:
        first_stage_id, _first_step_id, first_step = failed_steps[0]
        # Find the stage name
        stage_name = first_stage_id
        for s in execution.stages:
            if s.stage_id == first_stage_id:
                stage_name = s.name
                break
        lines.append("### Summary")
        lines.append(
            f"Pipeline `{execution.pipeline_name}` "
            f"{execution.status.value.upper()} at stage `{stage_name}`, "
            f"step `{first_step.name}`."
        )
    else:
        lines.append("### Summary")
        lines.append(
            f"Pipeline `{execution.pipeline_name}` status: "
            f"{execution.status.value.upper()}."
        )

    lines.append(
        f"Duration: {duration} | Branch: {branch_info} | Trigger: {trigger}"
    )
    lines.append("")

    # Root Cause
    if failed_steps:
        lines.append("### Root Cause")
        for stage_id, _step_id, step in failed_steps:
            stage_name = stage_id
            for s in execution.stages:
                if s.stage_id == stage_id:
                    stage_name = s.name
                    break
            ftypes = ""
            msg = ""
            if step.failure_info:
                ftypes = (
                    ", ".join(step.failure_info.failure_types)
                    if step.failure_info.failure_types
                    else "ERROR"
                )
                msg = step.failure_info.message
            lines.append(
                f"Step `{step.name}` in stage `{stage_name}` failed"
                + (f" with {ftypes}:" if ftypes else ":")
            )
            if msg:
                lines.append(f'"{msg}"')
        lines.append("")

    # Failed Step Details
    if step_logs:
        lines.append("### Failed Step Details")
        for key, detail_text in step_logs.items():
            parts = key.split("/", 1)
            step_label = parts[1] if len(parts) > 1 else key
            lines.append(f"\n**{step_label}:**")
            lines.append(detail_text)
        lines.append("")

    # Child Pipeline Findings
    if child_findings:
        lines.append("### Child Pipeline Details")
        for stage_id, findings in child_findings.items():
            stage_name = stage_id
            for s in execution.stages:
                if s.stage_id == stage_id:
                    stage_name = s.name
                    break

            child_ex = findings.get("child_execution")
            child_failed = findings.get("child_failed_steps", [])
            child_details = findings.get("child_step_details", {})

            if child_ex:
                child_dur = format_duration_ms(child_ex.start_ts, child_ex.end_ts)
                lines.append(
                    f"\n**{stage_name}** (Pipeline) — {status_icon(child_ex.status)} "
                    f"{child_ex.status.value} ({child_dur})"
                )
                lines.append(
                    f"↳ Child Pipeline: {child_ex.pipeline_name} "
                    f"(Run #{child_ex.run_sequence})"
                )

                if child_failed:
                    lines.append("")
                    lines.append("**Child Pipeline Failed At:**")
                    for c_stage_id, _c_step_id, c_step in child_failed:
                        c_stage_name = c_stage_id
                        for cs in child_ex.stages:
                            if cs.stage_id == c_stage_id:
                                c_stage_name = cs.name
                                break
                        c_dur = format_duration_ms(c_step.start_ts, c_step.end_ts)
                        lines.append(
                            f"  Stage: {c_stage_name} — Step: {c_step.name} "
                            f"({c_step.step_type}) — {status_icon(c_step.status)} "
                            f"{c_step.status.value} ({c_dur})"
                        )
                        if c_step.failure_info and c_step.failure_info.message:
                            lines.append(f'  Error: "{c_step.failure_info.message}"')
                elif child_ex.status in _FAILED_STATUSES:
                    # Failed but no step-level failures found
                    for cs in child_ex.stages:
                        if cs.status in _FAILED_STATUSES:
                            cs_dur = format_duration_ms(cs.start_ts, cs.end_ts)
                            lines.append(
                                f"  Failed Stage: {cs.name} — "
                                f"{status_icon(cs.status)} {cs.status.value} ({cs_dur})"
                            )
                            if cs.failure_info and cs.failure_info.message:
                                lines.append(f'  Error: "{cs.failure_info.message}"')
                elif child_ex.stages:
                    # Healthy child — compact overview of executed stages,
                    # with skipped stages collapsed to a single count
                    skipped_statuses = (ExecutionStatus.SKIPPED, ExecutionStatus.NOT_STARTED)
                    skipped_count = 0
                    for cs in child_ex.stages:
                        if cs.status in skipped_statuses:
                            skipped_count += 1
                            continue
                        cs_dur = format_duration_ms(cs.start_ts, cs.end_ts)
                        lines.append(
                            f"  {status_icon(cs.status)} {cs.name} ({cs_dur})"
                        )
                    if skipped_count:
                        lines.append(f"  ⏭️ {skipped_count} stage(s) skipped")

                # Show child step details
                if child_details:
                    for key, detail_text in child_details.items():
                        parts = key.split("/", 1)
                        step_label = parts[1] if len(parts) > 1 else key
                        lines.append(f"\n**Child Step — {step_label}:**")
                        lines.append(detail_text)

                if child_ex.execution_url:
                    lines.append(f"\nChild Execution: {child_ex.execution_url}")

        lines.append("")

    # Rollback Detection
    rollback_steps = _collect_rollback_steps(execution)
    if rollback_steps:
        lines.append("### Rollback Triggered")
        lines.append(
            f"⚠️ Deployment triggered **{len(rollback_steps)}** rollback step(s):"
        )
        for stage_id, _step_id, step in rollback_steps:
            stage_name = stage_id
            for s in execution.stages:
                if s.stage_id == stage_id:
                    stage_name = s.name
                    break
            icon = status_icon(step.status)
            dur = format_duration_ms(step.start_ts, step.end_ts)
            lines.append(
                f"  • **{step.name}** (Stage: {stage_name}) — "
                f"{icon} {step.status.value} ({dur})"
            )
        lines.append("")

    # Pipeline History
    if isinstance(history, list) and history:
        lines.append(f"### Pipeline History (last {len(history)} runs)")
        lines.append("| # | Status | Branch | When | Same failure? |")
        lines.append("|---|--------|--------|------|---------------|")

        # Determine the failed stages of the current execution for comparison
        current_failed_stages = {
            s.name for s in execution.stages if s.status in _FAILED_STATUSES
        }

        for hx in history:
            icon = status_icon(hx.status)
            branch = hx.branch or "—"
            when = format_relative_time(hx.start_ts)

            if hx.plan_execution_id == execution.plan_execution_id:
                same = "← THIS RUN"
            elif hx.status in _FAILED_STATUSES:
                hx_failed_stages = {
                    s.name for s in hx.stages if s.status in _FAILED_STATUSES
                }
                overlap = current_failed_stages & hx_failed_stages
                same = "Same stage" if overlap else "Different stage"
            else:
                same = "—"

            lines.append(
                f"| {hx.run_sequence} | {icon} {hx.status.value}"
                f" | {branch} | {when} | {same} |"
            )
        lines.append("")

    # Analysis
    lines.append("### Analysis")
    analysis = _build_analysis(execution, failed_steps, history)
    for item in analysis:
        lines.append(f"- {item}")
    lines.append("")

    # Deep Links
    if execution.execution_url:
        lines.append("### Deep Links")
        lines.append(f"- Execution: {execution.execution_url}")
        for stage_id, _step_id, step in failed_steps:
            stage_name = stage_id
            for s in execution.stages:
                if s.stage_id == stage_id:
                    stage_name = s.name
                    break
            lines.append(
                f"- Failed stage: {execution.execution_url}?stage={stage_name}"
            )
            lines.append(
                f"- Failed step: {execution.execution_url}?stage={stage_name}&step={step.name}"
            )

    return "\n".join(lines)


# K8s infrastructure keywords in error messages
_K8S_INFRA_KEYWORDS = (
    "insufficient", "unschedulable", "node affinity", "pod affinity",
    "taints", "tolerations", "progress deadline", "exceeded its progress deadline",
    "crashloopbackoff", "imagepullbackoff", "image pull",
    "oom", "out of memory", "steady state check failed",
    "no nodes available", "preemption", "scheduling",
    "scale-up", "node(s) were unschedulable",
)

# K8s deployment step types
_K8S_STEP_TYPES = ("k8s", "rolling", "canary", "bluegreen", "helm")


def _build_analysis(
    execution: PipelineExecution,
    failed_steps: list[tuple[str, str, StepExecution]],
    history: list[PipelineExecution] | BaseException,
) -> list[str]:
    """Build analysis bullet points from gathered data."""
    analysis: list[str] = []

    # Classify failure type — check message context, not just failure_types enum
    classified = False
    for _stage_id, _step_id, step in failed_steps:
        if classified:
            break

        # Gather all error text for keyword matching
        msg_lower = ""
        if step.failure_info:
            msg_lower = (step.failure_info.message or "").lower()
            for rm in step.failure_info.response_messages:
                msg_lower += " " + rm.lower()

        step_type_lower = (step.step_type or "").lower()
        is_k8s_step = any(t in step_type_lower for t in _K8S_STEP_TYPES)
        is_k8s_infra_issue = any(kw in msg_lower for kw in _K8S_INFRA_KEYWORDS)

        if is_k8s_infra_issue:
            analysis.append(
                "Failure is K8s infrastructure issue → check cluster resources, "
                "node scheduling, pod events, and autoscaler status"
            )
            classified = True
        elif is_k8s_step and step.failure_info:
            ftypes_lower = [ft.lower() for ft in (step.failure_info.failure_types or [])]
            if any("expired" in ft or "timeout" in ft for ft in ftypes_lower):
                analysis.append(
                    "Failure is a K8s deployment timeout → likely infrastructure issue "
                    "(resource pressure, scheduling delays, pod health checks)"
                )
                classified = True
            elif any("application" in ft for ft in ftypes_lower):
                analysis.append(
                    "Failure is in K8s deployment step with application error → "
                    "check pod health checks, resource limits, and cluster state"
                )
                classified = True
        elif step.failure_info and step.failure_info.failure_types:
            ftypes_lower = [ft.lower() for ft in step.failure_info.failure_types]
            if any("application" in ft for ft in ftypes_lower):
                analysis.append(
                    "Failure is in application/compilation stage → "
                    "code change issue, not infrastructure"
                )
                classified = True
            elif any("timeout" in ft for ft in ftypes_lower):
                analysis.append(
                    "Failure is a timeout → possible infrastructure or resource issue"
                )
                classified = True

    # History-based analysis
    if isinstance(history, list) and len(history) >= 2:
        current_failed_stages = {
            s.name for s in execution.stages if s.status in _FAILED_STATUSES
        }

        # Check if previous run on same branch succeeded
        for hx in history:
            if (
                hx.plan_execution_id != execution.plan_execution_id
                and hx.branch == execution.branch
                and hx.status == ExecutionStatus.SUCCESS
            ):
                analysis.append(
                    f"Previous run on same branch succeeded ({format_relative_time(hx.start_ts)}) "
                    f"→ recent code change likely cause"
                )
                break

        # Count recurring failures on same stage
        recurring_count = 0
        for hx in history:
            if hx.plan_execution_id == execution.plan_execution_id:
                continue
            if hx.status in _FAILED_STATUSES:
                hx_failed = {s.name for s in hx.stages if s.status in _FAILED_STATUSES}
                if current_failed_stages & hx_failed:
                    recurring_count += 1
        if recurring_count > 0:
            analysis.append(
                f"Run(s) #{recurring_count} also failed on same stage → possible recurring issue"
                + (f" on {execution.branch}" if execution.branch else "")
            )

    if not analysis:
        analysis.append("Insufficient history for trend analysis.")

    return analysis


# ------------------------------------------------------------------
# Comparison formatter
# ------------------------------------------------------------------


def _format_comparison(ex1: PipelineExecution, ex2: PipelineExecution) -> str:
    """Format a side-by-side comparison of two executions."""
    lines: list[str] = []
    lines.append(
        f"## Execution Comparison: "
        f"Run #{ex1.run_sequence} vs Run #{ex2.run_sequence}"
    )
    lines.append("")

    # Overview table
    lines.append("### Overview")
    lines.append("| | Run #" + str(ex1.run_sequence) + " | Run #" + str(ex2.run_sequence) + " |")
    lines.append("|---|---|---|")

    icon1 = status_icon(ex1.status)
    icon2 = status_icon(ex2.status)
    lines.append(f"| Status | {icon1} {ex1.status.value} | {icon2} {ex2.status.value} |")
    lines.append(f"| Branch | {ex1.branch or '—'} | {ex2.branch or '—'} |")
    lines.append(f"| Trigger | {ex1.trigger_type} | {ex2.trigger_type} |")
    lines.append(f"| Triggered by | {ex1.triggered_by or '—'} | {ex2.triggered_by or '—'} |")

    dur1 = format_duration_ms(ex1.start_ts, ex1.end_ts)
    dur2 = format_duration_ms(ex2.start_ts, ex2.end_ts)
    lines.append(f"| Duration | {dur1} | {dur2} |")

    when1 = format_relative_time(ex1.start_ts)
    when2 = format_relative_time(ex2.start_ts)
    lines.append(f"| When | {when1} | {when2} |")
    lines.append("")

    # Differences
    diffs = _compute_diffs(ex1, ex2)
    if diffs:
        lines.append("### Differences")
        for diff in diffs:
            lines.append(f"- {diff}")
        lines.append("")

    # Stage comparison
    lines.append("### Stage Comparison")
    lines.append(
        f"| Stage | Run #{ex1.run_sequence} | Run #{ex2.run_sequence} | Timing Δ |"
    )
    lines.append("|-------|---|---|----------|")

    stage_map_1 = {s.identifier: s for s in ex1.stages}
    stage_map_2 = {s.identifier: s for s in ex2.stages}
    all_stage_ids = list(dict.fromkeys(
        [s.identifier for s in ex1.stages] + [s.identifier for s in ex2.stages]
    ))

    for sid in all_stage_ids:
        s1 = stage_map_1.get(sid)
        s2 = stage_map_2.get(sid)
        name = (s1 or s2).name if (s1 or s2) else sid  # type: ignore[union-attr]

        if s1 and s2:
            st1 = f"{status_icon(s1.status)} {s1.status.value}"
            st2 = f"{status_icon(s2.status)} {s2.status.value}"
            delta = _timing_delta(s1, s2)
        elif s1:
            st1 = f"{status_icon(s1.status)} {s1.status.value}"
            st2 = "⏭️ Skipped"
            delta = "—"
        else:
            st1 = "⏭️ Skipped"
            st2 = f"{status_icon(s2.status)} {s2.status.value}"  # type: ignore[union-attr]
            delta = "—"

        lines.append(f"| {name} | {st1} | {st2} | {delta} |")

    return "\n".join(lines)


def _compute_diffs(ex1: PipelineExecution, ex2: PipelineExecution) -> list[str]:
    """Compute human-readable differences between two executions."""
    diffs: list[str] = []

    if ex1.status != ex2.status:
        diffs.append(
            f"**Status changed:** {ex1.status.value} → {ex2.status.value}"
        )

    if ex1.branch != ex2.branch:
        diffs.append(
            f"**Branch changed:** {ex1.branch or '—'} → {ex2.branch or '—'}"
        )

    if ex1.trigger_type != ex2.trigger_type:
        diffs.append(
            f"**Trigger type changed:** {ex1.trigger_type} → {ex2.trigger_type}"
        )

    if ex1.triggered_by != ex2.triggered_by:
        diffs.append(
            f"**Triggered by changed:** {ex1.triggered_by or '—'} → {ex2.triggered_by or '—'}"
        )

    # Stage-level diffs
    stage_ids_1 = {s.identifier for s in ex1.stages}
    stage_ids_2 = {s.identifier for s in ex2.stages}

    only_in_1 = stage_ids_1 - stage_ids_2
    only_in_2 = stage_ids_2 - stage_ids_1

    for sid in sorted(only_in_1):
        stage = next(s for s in ex1.stages if s.identifier == sid)
        diffs.append(f"**Stage only in Run #{ex1.run_sequence}:** {stage.name}")

    for sid in sorted(only_in_2):
        stage = next(s for s in ex2.stages if s.identifier == sid)
        diffs.append(f"**Stage only in Run #{ex2.run_sequence}:** {stage.name}")

    # Status changes for common stages
    for s1 in ex1.stages:
        for s2 in ex2.stages:
            if s1.identifier == s2.identifier and s1.status != s2.status:
                diffs.append(
                    f"**Stage `{s1.name}` status:** {s1.status.value} → {s2.status.value}"
                )

    return diffs


def _timing_delta(s1: StageExecution, s2: StageExecution) -> str:
    """Compute a timing delta string between two stages."""
    dur1 = _duration_ms(s1)
    dur2 = _duration_ms(s2)
    if dur1 is None or dur2 is None:
        return "—"
    diff = dur2 - dur1
    if abs(diff) < 1000:
        return "~same"
    sign = "+" if diff > 0 else "-"
    total_seconds = int(abs(diff)) // 1000
    if total_seconds < 60:
        return f"{sign}{total_seconds}s"
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{sign}{minutes}m {seconds}s"


def _duration_ms(stage: StageExecution) -> int | None:
    """Return stage duration in ms, or None if timestamps are missing."""
    if stage.start_ts is None or stage.end_ts is None:
        return None
    return stage.end_ts - stage.start_ts


# ------------------------------------------------------------------
# Watch execution formatters
# ------------------------------------------------------------------


def _format_watch_duration(seconds: float) -> str:
    """Human form of how long the watch ran."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60}s"


def _format_watch_success(
    execution: PipelineExecution, watched: str, polls: int
) -> str:
    duration = format_duration_ms(execution.start_ts, execution.end_ts)
    lines = [
        f"## ✅ Watch complete: {execution.pipeline_name} "
        f"(Run #{execution.run_sequence}) SUCCEEDED",
        f"Pipeline duration: {duration} | Watched for: {watched} ({polls} poll(s))",
        "",
        "### Stages",
    ]
    for stage in execution.stages:
        if stage.status in SKIPPED_STATUSES:
            continue
        dur = format_duration_ms(stage.start_ts, stage.end_ts)
        lines.append(f"  {status_icon(stage.status)} {stage.name} ({dur})")
    skipped = sum(1 for s in execution.stages if s.status in SKIPPED_STATUSES)
    if skipped:
        lines.append(f"  ⏭️ {skipped} stage(s) skipped")
    if execution.execution_url:
        lines.append("")
        lines.append(f"Harness: {execution.execution_url}")
    return "\n".join(lines)


def _format_watch_timeout(
    execution: PipelineExecution, watched: str, timeout_min: int, polls: int
) -> str:
    running_stage = next(
        (s for s in execution.stages if s.status == ExecutionStatus.RUNNING), None
    )
    lines = [
        f"## ⏱️ Watch timed out after {watched} "
        f"({polls} poll(s), limit {timeout_min}m)",
        f"{execution.pipeline_name} (Run #{execution.run_sequence}) is still "
        f"{status_icon(execution.status)} {execution.status.value}.",
    ]
    if running_stage:
        dur = format_duration_ms(running_stage.start_ts, None)
        lines.append(f"Currently in stage: **{running_stage.name}** ({dur} elapsed)")
    if execution.status in WAITING_STATUSES_FOR_WATCH:
        lines.append(
            "⚠️ The execution appears to be waiting on human action — "
            "check `hawkeye_check_approvals`."
        )
    lines.append("")
    lines.append("Re-run `hawkeye_watch_execution` to keep watching.")
    if execution.execution_url:
        lines.append(f"Harness: {execution.execution_url}")
    return "\n".join(lines)


WAITING_STATUSES_FOR_WATCH = frozenset({
    ExecutionStatus.APPROVAL_WAITING,
    ExecutionStatus.INTERVENTION_WAITING,
    ExecutionStatus.INPUT_WAITING,
})


# ------------------------------------------------------------------
# Flaky step analysis
# ------------------------------------------------------------------


class _StepStats:
    """Accumulated cross-run statistics for a single pipeline step."""

    __slots__ = ("stage_name", "step_name", "step_type", "runs", "fails",
                 "branches_run", "branches_failed", "errors")

    def __init__(self, stage_name: str, step_name: str, step_type: str) -> None:
        self.stage_name = stage_name
        self.step_name = step_name
        self.step_type = step_type
        self.runs = 0
        self.fails = 0
        self.branches_run: set[str] = set()
        self.branches_failed: set[str] = set()
        self.errors: dict[str, int] = {}


def _collect_step_stats(
    executions: list[PipelineExecution],
) -> dict[tuple[str, str], _StepStats]:
    """Aggregate per-step run/failure counts across executions."""
    stats: dict[tuple[str, str], _StepStats] = {}
    for ex in executions:
        branch = ex.branch or "(no branch)"
        for stage in ex.stages:
            if stage.status in SKIPPED_STATUSES:
                continue
            for step in stage.steps:
                if step.status in SKIPPED_STATUSES:
                    continue
                key = (stage.identifier, step.identifier or step.name)
                rec = stats.get(key)
                if rec is None:
                    rec = _StepStats(stage.name, step.name, step.step_type)
                    stats[key] = rec
                rec.runs += 1
                rec.branches_run.add(branch)
                if step.status in FAILED_STATUSES:
                    rec.fails += 1
                    rec.branches_failed.add(branch)
                    if step.failure_info and step.failure_info.message:
                        msg = step.failure_info.message
                        rec.errors[msg] = rec.errors.get(msg, 0) + 1
    return stats


def _branch_note(rec: _StepStats) -> str:
    """Describe whether failures are branch-specific."""
    if not rec.branches_failed:
        return "—"
    if len(rec.branches_failed) > 1:
        return f"{len(rec.branches_failed)} branches (regardless of branch)"
    only = next(iter(rec.branches_failed))
    if len(rec.branches_run) > 1:
        return f"only on `{only}`"
    return f"`{only}`"


def _format_flaky_steps(
    pipeline_id: str,
    executions: list[PipelineExecution],
) -> str:
    stats = _collect_step_stats(executions)

    flaky = sorted(
        (r for r in stats.values() if 0 < r.fails < r.runs and r.runs >= 3),
        key=lambda r: r.fails / r.runs,
        reverse=True,
    )
    broken = sorted(
        (r for r in stats.values() if r.fails == r.runs and r.runs >= 2),
        key=lambda r: r.runs,
        reverse=True,
    )

    name = executions[0].pipeline_name if executions else pipeline_id
    lines = [
        f"## Flaky Step Analysis: {name} (last {len(executions)} runs, "
        f"{len(stats)} distinct steps)",
        "",
    ]

    if flaky:
        lines.append("### 🎲 Flaky steps (fail intermittently)")
        lines.append("| Step | Stage | Fail rate | Failures | Branch pattern | Common error |")
        lines.append("|------|-------|-----------|----------|----------------|--------------|")
        for r in flaky:
            rate = r.fails / r.runs * 100
            common = max(r.errors, key=lambda k: r.errors[k]) if r.errors else "—"
            if len(common) > 60:
                common = common[:57] + "..."
            lines.append(
                f"| **{r.step_name}** | {r.stage_name} | {rate:.0f}% "
                f"({r.fails}/{r.runs}) | {r.fails} | {_branch_note(r)} | {common} |"
            )
        lines.append("")

    if broken:
        lines.append("### 💥 Consistently broken steps (fail every run)")
        lines.append("| Step | Stage | Runs | Branch pattern | Common error |")
        lines.append("|------|-------|------|----------------|--------------|")
        for r in broken:
            common = max(r.errors, key=lambda k: r.errors[k]) if r.errors else "—"
            if len(common) > 60:
                common = common[:57] + "..."
            lines.append(
                f"| **{r.step_name}** | {r.stage_name} | {r.runs} "
                f"| {_branch_note(r)} | {common} |"
            )
        lines.append("")

    if not flaky and not broken:
        total_failing = sum(1 for r in stats.values() if r.fails)
        if total_failing:
            lines.append(
                f"No flaky or consistently-broken steps detected — "
                f"{total_failing} step(s) failed only once each in this window."
            )
        else:
            lines.append("✅ No step failures at all in this window.")
    else:
        lines.append(
            "💡 Flaky = fails sometimes under the same conditions (ran ≥3×, "
            "failed at least once but not always). Investigate a specific "
            "failure with `hawkeye_diagnose`."
        )

    return "\n".join(lines)


# ------------------------------------------------------------------
# Failure pattern formatter
# ------------------------------------------------------------------


def _format_failure_pattern(
    pipeline_id: str,
    executions: list[PipelineExecution],
) -> str:
    """Analyze executions and produce a failure pattern report."""
    total = len(executions)
    success_count = sum(1 for e in executions if e.status == ExecutionStatus.SUCCESS)
    failed_count = sum(1 for e in executions if e.status in _FAILED_STATUSES)
    aborted_count = sum(1 for e in executions if e.status == ExecutionStatus.ABORTED)

    name = executions[0].pipeline_name if executions else pipeline_id
    success_rate = (success_count / total * 100) if total > 0 else 0

    lines: list[str] = []
    lines.append(f"## Failure Pattern Analysis: {name} (last {total} runs)")
    lines.append("")

    # Overall
    lines.append("### Overall")
    lines.append(f"Success rate: {success_rate:.0f}% ({success_count}/{total})")
    lines.append(f"Failed: {failed_count} | Aborted: {aborted_count}")
    lines.append("")

    # Failure hotspots
    stage_failures: dict[str, list[str]] = {}  # stage_name -> [error_messages]
    for ex in executions:
        if ex.status not in _FAILED_STATUSES:
            continue
        for stage in ex.stages:
            if stage.status not in _FAILED_STATUSES:
                continue
            msg = ""
            if stage.failure_info and stage.failure_info.message:
                msg = stage.failure_info.message
            stage_failures.setdefault(stage.name, []).append(msg)

    if stage_failures:
        lines.append("### Failure Hotspots")
        lines.append("| Stage | Failures | % of all failures | Common error |")
        lines.append("|-------|----------|-------------------|--------------|")

        # Sort by failure count descending
        sorted_stages = sorted(
            stage_failures.items(), key=lambda kv: len(kv[1]), reverse=True
        )
        for stage_name, errors in sorted_stages:
            count = len(errors)
            pct = (count / failed_count * 100) if failed_count > 0 else 0
            # Most common non-empty error
            non_empty = [e for e in errors if e]
            common_error = _most_common(non_empty) if non_empty else "—"
            # Truncate long error messages
            if len(common_error) > 60:
                common_error = common_error[:57] + "..."
            lines.append(
                f"| {stage_name} | {count} | {pct:.0f}% | {common_error} |"
            )
        lines.append("")

    # Patterns
    patterns = _detect_patterns(executions, stage_failures)
    if patterns:
        lines.append("### Patterns Detected")
        for p in patterns:
            lines.append(f"- {p}")
    else:
        lines.append("### Patterns Detected")
        lines.append("- No recurring patterns detected.")

    return "\n".join(lines)


def _most_common(items: list[str]) -> str:
    """Return the most frequent item in a list."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else ""


def _detect_patterns(
    executions: list[PipelineExecution],
    stage_failures: dict[str, list[str]],
) -> list[str]:
    """Detect recurring patterns from execution history."""
    patterns: list[str] = []

    # Recurring stage failures
    for stage_name, errors in stage_failures.items():
        if len(errors) >= 2:
            non_empty = [e for e in errors if e]
            # Check if same error message repeats
            if non_empty:
                common = _most_common(non_empty)
                same_count = sum(1 for e in non_empty if e == common)
                if same_count >= 2:
                    short_err = common[:40] + "..." if len(common) > 40 else common
                    patterns.append(
                        f"⚠️ RECURRING: `{stage_name}` has failed {len(errors)} times — "
                        f"all {short_err}" if same_count == len(non_empty) else
                        f"⚠️ RECURRING: `{stage_name}` has failed {len(errors)} times — "
                        f"{same_count}x {short_err}"
                    )
                else:
                    patterns.append(
                        f"⚠️ RECURRING: `{stage_name}` has failed {len(errors)} times"
                    )
            else:
                patterns.append(
                    f"⚠️ RECURRING: `{stage_name}` has failed {len(errors)} times"
                )

    # Branch-specific failures
    branch_failures: dict[str, list[str]] = {}  # branch -> [stage_names]
    for ex in executions:
        if ex.status not in _FAILED_STATUSES:
            continue
        branch = ex.branch or "—"
        for stage in ex.stages:
            if stage.status in _FAILED_STATUSES:
                branch_failures.setdefault(branch, []).append(stage.name)

    for branch, stage_names in branch_failures.items():
        # Check if all failures of a particular stage are on this branch
        for stage_name in set(stage_names):
            all_failures_for_stage = stage_failures.get(stage_name, [])
            branch_count = stage_names.count(stage_name)
            if branch_count == len(all_failures_for_stage) and branch_count >= 2:
                patterns.append(
                    f"🔍 BRANCH-SPECIFIC: All `{stage_name}` failures on `{branch}`"
                )

    # Success trend
    if len(executions) >= 3:
        last_3 = executions[:3]
        if all(e.status == ExecutionStatus.SUCCESS for e in last_3):
            patterns.append("📊 SUCCESS TREND: Last 3 runs all succeeded (improving)")
        elif all(e.status in _FAILED_STATUSES for e in last_3):
            patterns.append("📊 FAILURE TREND: Last 3 runs all failed (degrading)")

    return patterns
