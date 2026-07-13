"""Shared failure-investigation data gathering.

One home for the orchestration that used to be copy-pasted across
``hawkeye_diagnose``, ``hawkeye_investigate_failure``,
``hawkeye_get_all_stages``, ``hawkeye_get_child_execution``, and the child
pipeline follower: collecting failed steps, fetching their details in
parallel, enriching CI infrastructure nodes, and following child pipelines.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.models import (
    FAILED_STATUSES,
    ExecutionStatus,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from providers.base import PipelineProvider
from providers.harness.graph_search import extract_stage_identifier
from providers.harness.parser import (
    extract_child_execution_id,
    extract_node_details,
    truncate_to_tail,
)

logger = logging.getLogger(__name__)

# Step types that represent child pipeline calls
_PIPELINE_STEP_TYPES = {"Pipeline", "PipelineStage"}

# CI infrastructure node types that indicate pod initialization
_CI_INFRA_STEP_TYPES = {"liteEngineTask"}

_MAX_CHILD_STEP_DETAILS = 5


def is_pipeline_step_type(step_type: str) -> bool:
    """Return True if step_type represents a child pipeline call."""
    if step_type in _PIPELINE_STEP_TYPES:
        return True
    return "pipeline" in step_type.lower() if step_type else False


def collect_failed_steps(
    execution: PipelineExecution,
) -> list[tuple[str, str, StepExecution]]:
    """Return ``(stage_id, step_id, step)`` for every failed/expired step."""
    failed: list[tuple[str, str, StepExecution]] = []
    for stage in execution.stages:
        if stage.status not in FAILED_STATUSES:
            continue
        for step in stage.steps:
            if step.status in FAILED_STATUSES:
                failed.append((stage.stage_id, step.step_id, step))
    return failed


def append_console_logs(details: str, raw_logs: str, source: str) -> str:
    """Append truncated console logs to an existing step detail block."""
    logs = truncate_to_tail(raw_logs)
    return (
        f"{details}\n\n"
        f"**Console Logs** ({source}):\n"
        f"```\n{logs}\n```"
    )


def format_step_detail_result(
    raw: Any,
    *,
    include_logs: bool = True,
    unavailable_note: str = "(step details unavailable)",
) -> str:
    """Render one ``get_step_logs`` result into a detail block."""
    if isinstance(raw, BaseException) or not isinstance(raw, dict):
        return unavailable_note
    node_data = raw.get("node")
    subgraph_data = raw.get("subgraph")
    if not node_data:
        return unavailable_note
    detail = extract_node_details(node_data, subgraph_data)
    if include_logs:
        api_logs = raw.get("api_logs")
        if api_logs:
            detail = append_console_logs(
                detail, str(api_logs), "Harness Log Service API"
            )
    return detail


async def build_step_details(
    provider: PipelineProvider,
    execution_id: str,
    step_refs: list[tuple[str, str]],
    *,
    include_logs: bool = True,
    unavailable_note: str = "(step details unavailable)",
) -> dict[str, str]:
    """Fetch details for the given ``(stage_id, step_id)`` refs in parallel.

    Returns a mapping keyed ``"{stage_id}/{step_id}"``.  Individual fetch
    failures degrade to *unavailable_note*; this never raises.
    """
    if not step_refs:
        return {}
    tasks = [
        provider.get_step_logs(execution_id, stage_id, step_id)
        for stage_id, step_id in step_refs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    details: dict[str, str] = {}
    for (stage_id, step_id), raw in zip(step_refs, results, strict=True):
        details[f"{stage_id}/{step_id}"] = format_step_detail_result(
            raw, include_logs=include_logs, unavailable_note=unavailable_note
        )
    return details


def enrich_ci_infra(
    execution: PipelineExecution,
    covered_stage_ids: set[str],
    detail_map: dict[str, str],
    raw_node_map: dict,
) -> None:
    """Add liteEngineTask details for failed stages without step-level detail.

    CI stages that expire during pod initialization have no failed steps —
    the failure lives on the liteEngineTask (Initialize) node in the raw
    graph.  Mutates *detail_map* in place, keyed
    ``"{stage_id}/liteEngineTask"``.
    """
    uncovered_stages = [
        s for s in execution.stages
        if s.status in FAILED_STATUSES and s.stage_id not in covered_stage_ids
    ]
    if not uncovered_stages:
        return

    uncovered_identifiers = {s.identifier for s in uncovered_stages}

    for _uuid, raw_node in raw_node_map.items():
        if not isinstance(raw_node, dict):
            continue
        if raw_node.get("stepType", "") not in _CI_INFRA_STEP_TYPES:
            continue
        if raw_node.get("status", "") not in ("Failed", "Errored", "Expired"):
            continue

        stage_identifier = extract_stage_identifier(raw_node.get("baseFqn", ""))
        if stage_identifier not in uncovered_identifiers:
            continue

        matching_stage = next(
            (s for s in uncovered_stages if s.identifier == stage_identifier), None
        )
        if not matching_stage:
            continue

        detail_map[f"{matching_stage.stage_id}/liteEngineTask"] = (
            extract_node_details(raw_node)
        )


# ------------------------------------------------------------------
# Child pipeline following
# ------------------------------------------------------------------


def find_child_execution_id(
    stage: StageExecution,
    raw_node_map: dict | None,
) -> str | None:
    """Find the child execution ID for a Pipeline-type stage.

    Searches the raw node map for nodes belonging to this stage that
    contain child execution references.
    """
    if not raw_node_map or not isinstance(raw_node_map, dict):
        return None

    for _uuid, raw_node in raw_node_map.items():
        if not isinstance(raw_node, dict):
            continue

        base_fqn = raw_node.get("baseFqn", "")
        node_name = raw_node.get("name", "")
        node_identifier = raw_node.get("identifier", "")
        step_type = raw_node.get("stepType", "")

        stage_match = (
            stage.identifier in base_fqn
            or node_name == stage.name
            or node_identifier == stage.identifier
        )
        if not stage_match:
            continue
        if not is_pipeline_step_type(step_type):
            continue

        child_id = extract_child_execution_id(raw_node)
        if child_id:
            return child_id

    return None


async def follow_child_pipelines(
    execution: PipelineExecution,
    raw_node_map: dict | None,
    provider: PipelineProvider,
) -> dict[str, dict]:
    """Follow child pipeline executions for Pipeline-type (chained) stages.

    ALL executed Pipeline-type stages are followed — orchestrator pipelines
    need child visibility whether the run failed or succeeded.  Deep step
    details are fetched only for failed steps inside each child.

    Returns a dict mapping stage_id -> child execution findings:
    {
        "child_execution": PipelineExecution,
        "child_failed_steps": [(stage_id, step_id, step)],
        "child_step_details": {"stage/step": "detail text"},
    }

    Only follows ONE level deep (no grandchild pipelines).
    Best-effort: never raises.
    """
    findings: dict[str, dict] = {}

    pipeline_stages = [
        s for s in execution.stages
        if is_pipeline_step_type(s.stage_type)
        and s.status not in (ExecutionStatus.SKIPPED, ExecutionStatus.NOT_STARTED)
    ]

    if not pipeline_stages:
        logger.debug("follow_child: no Pipeline-type stages")
        return findings

    for stage in pipeline_stages:
        try:
            # The layout node carries the authoritative child execution ID
            # (stepDetails.childPipelineExecutionDetails); fall back to
            # scanning the raw execution graph.
            child_id = stage.child_execution_id or find_child_execution_id(
                stage, raw_node_map
            )
            logger.debug("follow_child: stage=%s, child_id=%s", stage.name, child_id)
            if not child_id:
                continue

            child_execution = await provider.get_execution(child_id)

            child_failed = collect_failed_steps(child_execution)
            logger.debug(
                "follow_child: %s has %d failed steps (stages=%d)",
                stage.name, len(child_failed), len(child_execution.stages),
            )

            child_step_details = await build_step_details(
                provider,
                child_id,
                [(sid, stid) for sid, stid, _step in child_failed[:_MAX_CHILD_STEP_DETAILS]],
                unavailable_note="(details unavailable)",
            )

            findings[stage.stage_id] = {
                "child_execution": child_execution,
                "child_failed_steps": child_failed,
                "child_step_details": child_step_details,
            }
        except Exception:
            logger.debug("follow_child: exception for stage %s", stage.name, exc_info=True)

    return findings


# ------------------------------------------------------------------
# Full investigation orchestration
# ------------------------------------------------------------------


async def gather_investigation(
    provider: PipelineProvider,
    execution_id: str,
    execution: PipelineExecution,
    *,
    include_logs: bool = True,
) -> tuple[
    list[PipelineExecution] | BaseException,
    dict | None,
    dict[str, str],
    dict[str, dict],
]:
    """Run the full parallel investigation for one execution.

    Fetches pipeline history, the raw execution graph, and details for all
    failed steps in parallel; enriches CI infra nodes; follows child
    pipelines.  Returns ``(history, raw_node_map, step_details,
    child_findings)``.
    """
    failed_steps = collect_failed_steps(execution)

    step_detail_tasks = [
        provider.get_step_logs(execution_id, stage_id, step_id)
        for stage_id, step_id, _step in failed_steps
    ]
    history_task = provider.list_recent_executions(
        execution.pipeline_identifier, limit=5
    )
    raw_graph_task = provider.get_raw_execution_graph(execution_id)

    results = await asyncio.gather(
        history_task,
        raw_graph_task,
        *step_detail_tasks,
        return_exceptions=True,
    )

    history = results[0] if not isinstance(results[0], BaseException) else []
    raw_node_map = results[1] if not isinstance(results[1], BaseException) else None
    detail_results = results[2:]

    step_details: dict[str, str] = {}
    for i, (stage_id, step_id, _step) in enumerate(failed_steps):
        step_details[f"{stage_id}/{step_id}"] = format_step_detail_result(
            detail_results[i], include_logs=include_logs
        )

    if raw_node_map is not None and isinstance(raw_node_map, dict):
        covered = {stage_id for stage_id, _step_id, _step in failed_steps}
        enrich_ci_infra(execution, covered, step_details, raw_node_map)

    child_findings = await follow_child_pipelines(execution, raw_node_map, provider)

    return history, raw_node_map, step_details, child_findings
