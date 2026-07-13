"""Execution tools — the core tools that replace manual copy-paste workflow.

8 tools:
- hawkeye_get_execution
- hawkeye_get_stage_detail
- hawkeye_get_step_logs
- hawkeye_get_execution_inputs
- hawkeye_list_recent_executions
- hawkeye_get_all_stages
- hawkeye_get_child_execution
- hawkeye_check_approvals
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

from core.models import ExecutionStatus, PipelineExecution, StageExecution
from providers.harness.parser import extract_node_details, truncate_to_tail
from providers.harness.url_patterns import parse_harness_url
from tools._helpers import (
    format_duration_ms,
    format_relative_time,
    get_provider,
    resolve_pipeline_id,
    run_tool,
    status_icon,
    status_label,
)

_MAX_LOG_LINES = 200

# Failure statuses that may benefit from console log retrieval
_FAILED_STATUSES = frozenset({
    ExecutionStatus.FAILED,
    ExecutionStatus.ERRORED,
    ExecutionStatus.EXPIRED,
})

# Step types that represent child pipeline calls
_PIPELINE_STEP_TYPES = {"Pipeline", "PipelineStage"}


def _is_pipeline_step_type(step_type: str) -> bool:
    """Return True if step_type represents a child pipeline call."""
    if step_type in _PIPELINE_STEP_TYPES:
        return True
    return "pipeline" in step_type.lower() if step_type else False


def register_execution_tools(mcp: FastMCP) -> None:
    """Register all execution tools on the MCP server."""

    @mcp.tool()
    async def hawkeye_get_execution(
        execution_id: str = "",
        url: str = "",
    ) -> str:
        """Get full pipeline execution details — status, stages, failures, and Harness deep link.

        Accepts either an execution_id or a Harness UI URL (preferred — just paste the URL).
        Returns pipeline name, status, duration, trigger info, all stages with status,
        and failure details for any failed stages.
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
            return _format_execution(execution)

        return await run_tool("hawkeye_get_execution", params, _run())

    @mcp.tool()
    async def hawkeye_get_stage_detail(
        execution_id: str = "",
        stage_id: str = "",
        url: str = "",
    ) -> str:
        """Get detailed step-by-step breakdown of a pipeline stage.

        Returns every step within the stage — name, status, duration, delegate info,
        step type, and failure details for failed steps.
        Accepts execution_id + stage_id, or a Harness URL with stage query param.
        """
        params = {"execution_id": execution_id, "stage_id": stage_id, "url": url}

        async def _run() -> str:
            eid = execution_id
            sid = stage_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
                if parsed.stage_id:
                    sid = parsed.stage_id
            if not eid:
                return "Error: Provide execution_id or a Harness URL."
            if not sid:
                return "Error: Provide stage_id or a Harness URL with ?stage= parameter."

            provider = await get_provider()
            execution = await provider.get_execution(eid)
            stage = _find_stage(execution, sid)
            if stage is None:
                return f"Error: Stage '{sid}' not found in execution '{eid}'."
            return _format_stage_detail(execution, stage)

        return await run_tool("hawkeye_get_stage_detail", params, _run())

    @mcp.tool()
    async def hawkeye_get_step_logs(
        execution_id: str = "",
        stage_id: str = "",
        step_id: str = "",
        url: str = "",
    ) -> str:
        """Get detailed step execution information including script source,
        outputs, and failure details.

        Extracts step data from the Harness execution graph — shows what
        script was executed, what it output, and why it failed.

        Provide either execution_id + stage_id + step_id, or a Harness URL
        with stage and step parameters.
        """
        params = {
            "execution_id": execution_id,
            "stage_id": stage_id,
            "step_id": step_id,
            "url": url,
        }

        async def _run() -> str:
            eid = execution_id
            sid = stage_id
            stid = step_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
                if parsed.stage_id:
                    sid = parsed.stage_id
                if parsed.step_id:
                    stid = parsed.step_id
            if not eid:
                return "Error: Provide execution_id or a Harness URL."
            if not sid:
                return "Error: Provide stage_id or a Harness URL with ?stage= parameter."
            if not stid:
                return "Error: Provide step_id or a Harness URL with ?step= parameter."

            provider = await get_provider()
            result = await provider.get_step_logs(eid, sid, stid)
            node_data = result.get("node")
            subgraph_data = result.get("subgraph")

            if node_data is None:
                lines = [f"⚠️ Step `{stid}` not found in stage `{sid}`."]
                available = result.get("available_nodes", [])
                if available:
                    lines.append("")
                    lines.append("Available steps in this execution:")
                    for n in available:
                        icon = status_icon(
                            ExecutionStatus.from_str(n.get("status", "Unknown"))
                        )
                        fqn = n.get("base_fqn") or ""
                        # Extract stage name from base_fqn: pipeline.stages.<stage>.spec...
                        stage_hint = ""
                        if fqn:
                            parts = fqn.split(".")
                            try:
                                idx = parts.index("stages")
                                if idx + 1 < len(parts):
                                    stage_hint = f" [stage: {parts[idx + 1]}]"
                            except ValueError:
                                pass
                        lines.append(
                            f"  • {n['identifier']} ({n['step_type']}) "
                            f"— {icon} {n['status']}{stage_hint}"
                        )
                return "\n".join(lines)

            # Tier 1: Graph/subgraph data (always available)
            details = extract_node_details(node_data, subgraph_data)

            # Tier 2: Official Harness Log Service API console logs
            api_logs = result.get("api_logs")
            if api_logs:
                api_logs = truncate_to_tail(str(api_logs))

            # Combine graph data + console logs
            header = f"## Step Details: `{stid}`\nExecution: `{eid}` | Stage: `{sid}`\n\n"
            if api_logs:
                return (
                    f"{header}{details}\n\n"
                    f"### Console Logs (Harness Log Service API)\n"
                    f"```\n{api_logs}\n```"
                )
            return f"{header}{details}"

        return await run_tool("hawkeye_get_step_logs", params, _run())

    @mcp.tool()
    async def hawkeye_get_execution_inputs(
        execution_id: str = "",
        url: str = "",
    ) -> str:
        """Get the input YAML used for a specific pipeline execution run.

        Returns the runtime variables and input sets that were used when this
        execution was triggered.
        """
        params = {"execution_id": execution_id, "url": url}

        async def _run() -> str:
            eid = execution_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
            if not eid:
                return "Error: Provide execution_id or a Harness URL."

            provider = await get_provider()
            yaml_content = await provider.get_execution_inputs(eid)
            if not yaml_content or not yaml_content.strip():
                return f"No input YAML found for execution `{eid}`."
            return f"## Execution Inputs: `{eid}`\n\n```yaml\n{yaml_content}\n```"

        return await run_tool("hawkeye_get_execution_inputs", params, _run())

    @mcp.tool()
    async def hawkeye_list_recent_executions(
        pipeline_id: str,
        limit: int = 10,
        status: str | None = None,
    ) -> str:
        """List recent executions for a pipeline.

        Summary table with status, branch, trigger, duration.

        Useful for seeing execution history at a glance. Filter by status (e.g. "Failed")
        to find recent failures.
        """
        params = {"pipeline_id": pipeline_id, "limit": limit, "status": status}

        async def _run() -> str:
            resolved_id = await resolve_pipeline_id(pipeline_id)
            status_filter = None
            if status:
                status_filter = ExecutionStatus.from_str(status)

            provider = await get_provider()
            executions = await provider.list_recent_executions(
                resolved_id, limit=limit, status=status_filter
            )
            if not executions:
                msg = f"No executions found for pipeline `{pipeline_id}`"
                if status:
                    msg += f" with status '{status}'"
                return msg + "."

            return _format_execution_table(pipeline_id, executions, limit)

        return await run_tool("hawkeye_list_recent_executions", params, _run())

    @mcp.tool()
    async def hawkeye_get_all_stages(
        execution_id: str = "",
        url: str = "",
    ) -> str:
        """Get ALL stages with their steps in ONE call.

        Returns every stage in the execution with step-level detail. For failed
        steps, includes full error info (script source, outputs, failure details).
        Consecutive skipped stages (>3) are collapsed.

        Accepts either an execution_id or a Harness URL.
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

            if not execution.stages:
                return f"No stages found in execution `{eid}`."

            # Collect failed/expired steps that need detailed info
            failed_step_tasks: list[tuple[int, int, str, str]] = []  # (stage_idx, step_idx, stage_id, step_id)
            for si, stage in enumerate(execution.stages):
                if stage.status in (ExecutionStatus.SKIPPED, ExecutionStatus.NOT_STARTED):
                    continue
                for sti, step in enumerate(stage.steps):
                    if step.status in _FAILED_STATUSES:
                        failed_step_tasks.append((si, sti, stage.stage_id, step.step_id))

            # Fetch details for failed steps in parallel + raw graph for CI infra enrichment
            detail_map: dict[str, str] = {}
            step_tasks = [
                provider.get_step_logs(eid, stage_id, step_id)
                for _si, _sti, stage_id, step_id in failed_step_tasks
            ]
            raw_graph_task = provider.get_raw_execution_graph(eid)
            all_results = await asyncio.gather(
                raw_graph_task, *step_tasks, return_exceptions=True
            )
            raw_node_map = all_results[0] if not isinstance(all_results[0], BaseException) else None
            step_results = all_results[1:]

            for i, (_si, _sti, stage_id, step_id) in enumerate(failed_step_tasks):
                raw = step_results[i]
                if isinstance(raw, BaseException):
                    detail_map[f"{stage_id}/{step_id}"] = "(details unavailable)"
                else:
                    node_data = raw.get("node") if isinstance(raw, dict) else None
                    subgraph_data = raw.get("subgraph") if isinstance(raw, dict) else None
                    if node_data:
                        detail = extract_node_details(node_data, subgraph_data)
                        api_logs = raw.get("api_logs")
                        if api_logs:
                            detail = _append_console_logs(
                                detail,
                                str(api_logs),
                                "Harness Log Service API",
                            )
                        detail_map[f"{stage_id}/{step_id}"] = detail
                    else:
                        detail_map[f"{stage_id}/{step_id}"] = "(details unavailable)"

            # Enrich with CI infra nodes for expired stages that had no failed steps
            if raw_node_map and isinstance(raw_node_map, dict):
                _enrich_ci_infra_from_raw_graph(execution, failed_step_tasks, detail_map, raw_node_map)

            return _format_all_stages(execution, detail_map)

        return await run_tool("hawkeye_get_all_stages", params, _run())

    @mcp.tool()
    async def hawkeye_get_child_execution(
        execution_id: str = "",
        stage_id: str = "",
        url: str = "",
    ) -> str:
        """Follow a child pipeline execution from a Pipeline-type stage.

        When a pipeline has stages that call other pipelines (chained/child
        pipelines), this tool drills into the child execution to find the
        actual failure details.

        Provide the parent execution_id + stage_id of the Pipeline-type stage,
        or a Harness URL.
        """
        params = {"execution_id": execution_id, "stage_id": stage_id, "url": url}

        async def _run() -> str:
            from providers.harness.parser import extract_child_execution_id

            eid = execution_id
            sid = stage_id
            if url:
                parsed = parse_harness_url(url)
                if parsed.execution_id:
                    eid = parsed.execution_id
                if parsed.stage_id:
                    sid = parsed.stage_id
            if not eid:
                return "Error: Provide execution_id or a Harness URL."
            if not sid:
                return "Error: Provide stage_id for the Pipeline-type stage."

            provider = await get_provider()

            # Get parent execution and raw graph
            execution, raw_node_map = await asyncio.gather(
                provider.get_execution(eid),
                provider.get_raw_execution_graph(eid),
                return_exceptions=False,
            )

            # Find the target stage
            stage = _find_stage(execution, sid)
            if stage is None:
                return f"Error: Stage '{sid}' not found in execution '{eid}'."

            if not _is_pipeline_step_type(stage.stage_type):
                return (
                    f"Stage '{stage.name}' is type '{stage.stage_type}', "
                    f"not a Pipeline-type stage."
                )

            # The layout node carries the authoritative child execution ID;
            # fall back to searching the raw graph.
            child_id = stage.child_execution_id
            if not child_id and isinstance(raw_node_map, dict):
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
                    if not stage_match or not _is_pipeline_step_type(step_type):
                        continue

                    child_id = extract_child_execution_id(raw_node)
                    if child_id:
                        break

            if not child_id:
                return (
                    f"Could not find child execution ID for stage '{stage.name}'.\n"
                    f"The child pipeline reference may not be in the execution graph.\n"
                    f"Try opening the stage in the Harness UI to find the child execution."
                )

            # Get child execution
            child_execution = await provider.get_execution(child_id)

            # Build report
            lines = _format_child_execution(execution, stage, child_execution)

            # Get failed step details from child
            from providers.harness.parser import extract_node_details as _extract_details

            failed_in_child: list[tuple[str, str]] = []
            for cs in child_execution.stages:
                if cs.status not in _FAILED_STATUSES:
                    continue
                for step in cs.steps:
                    if step.status in _FAILED_STATUSES:
                        failed_in_child.append((cs.stage_id, step.step_id))

            if failed_in_child:
                detail_tasks = [
                    provider.get_step_logs(child_id, csid, cstid)
                    for csid, cstid in failed_in_child[:5]
                ]
                results = await asyncio.gather(*detail_tasks, return_exceptions=True)
                for i, (_csid, cstid) in enumerate(failed_in_child[:5]):
                    raw = results[i]
                    if isinstance(raw, BaseException):
                        continue
                    node_data = raw.get("node") if isinstance(raw, dict) else None
                    subgraph_data = raw.get("subgraph") if isinstance(raw, dict) else None
                    if node_data:
                        lines.append("")
                        lines.append(f"**Failed Step Details — {cstid}:**")
                        detail = _extract_details(node_data, subgraph_data)
                        api_logs = raw.get("api_logs") if isinstance(raw, dict) else None
                        if api_logs:
                            detail = _append_console_logs(
                                detail,
                                str(api_logs),
                                "Harness Log Service API",
                            )
                        lines.append(detail)

            return "\n".join(lines)

        return await run_tool("hawkeye_get_child_execution", params, _run())

    @mcp.tool()
    async def hawkeye_check_approvals(
        execution_id: str = "",
        url: str = "",
    ) -> str:
        """Check pending approval gates in a pipeline execution.

        Shows all approval steps — pending, approved, and rejected — with
        approver info, approval message, and timestamps. Useful for checking
        why a pipeline appears stuck.

        Accepts either an execution_id or a Harness URL.
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
            execution, raw_node_map = await asyncio.gather(
                provider.get_execution(eid),
                provider.get_raw_execution_graph(eid),
            )

            return _format_approvals(execution, raw_node_map)

        return await run_tool("hawkeye_check_approvals", params, _run())


# ------------------------------------------------------------------
# Approval helpers
# ------------------------------------------------------------------

_APPROVAL_STATUSES = frozenset({
    ExecutionStatus.APPROVAL_WAITING,
    ExecutionStatus.APPROVAL_REJECTED,
})


def _is_approval_step_type(step_type: str) -> bool:
    """Return True if step_type represents an approval step."""
    return "approval" in step_type.lower() if step_type else False


def _find_approval_nodes(
    execution: PipelineExecution,
    raw_node_map: dict,
) -> list[dict]:
    """Collect all approval-related nodes from the execution and raw graph.

    Returns a list of dicts with keys:
    name, stage_name, status, step_type, start_ts, end_ts, approvers, message
    """
    approvals: list[dict] = []
    seen_ids: set[str] = set()

    # Pass 1: scan parsed execution stages/steps
    for stage in execution.stages:
        for step in stage.steps:
            if step.status in _APPROVAL_STATUSES or _is_approval_step_type(step.step_type):
                seen_ids.add(step.step_id)
                raw_node = raw_node_map.get(step.step_id, {}) if isinstance(raw_node_map, dict) else {}
                approvers, message = _extract_approval_info(raw_node)
                approvals.append({
                    "name": step.name,
                    "stage_name": stage.name,
                    "status": step.status,
                    "step_type": step.step_type,
                    "start_ts": step.start_ts,
                    "end_ts": step.end_ts,
                    "approvers": approvers,
                    "message": message,
                })

    # Pass 2: scan raw node map for approval nodes not found in parsed steps
    if isinstance(raw_node_map, dict):
        for uuid, raw_node in raw_node_map.items():
            if not isinstance(raw_node, dict) or uuid in seen_ids:
                continue
            step_type = raw_node.get("stepType", "")
            raw_status_str = raw_node.get("status", "")
            raw_status = ExecutionStatus.from_str(raw_status_str)
            if raw_status in _APPROVAL_STATUSES or _is_approval_step_type(step_type):
                approvers, message = _extract_approval_info(raw_node)
                # Determine stage name from baseFqn
                stage_name = _stage_name_from_fqn(raw_node.get("baseFqn", ""))
                approvals.append({
                    "name": raw_node.get("name", uuid),
                    "stage_name": stage_name,
                    "status": raw_status,
                    "step_type": step_type,
                    "start_ts": raw_node.get("startTs"),
                    "end_ts": raw_node.get("endTs"),
                    "approvers": approvers,
                    "message": message,
                })

    return approvals


def _extract_approval_info(raw_node: dict) -> tuple[list[str], str]:
    """Extract approver groups/users and approval message from a raw node."""
    approvers: list[str] = []
    message = ""

    step_params = raw_node.get("stepParameters", {})
    if not isinstance(step_params, dict):
        return approvers, message

    message = str(step_params.get("approvalMessage", ""))

    approvers_block = step_params.get("approvers", {})
    if isinstance(approvers_block, dict):
        user_groups = approvers_block.get("userGroups", [])
        if isinstance(user_groups, list):
            for ug in user_groups:
                if isinstance(ug, str):
                    approvers.append(ug)
                elif isinstance(ug, dict):
                    approvers.append(str(ug.get("name", ug.get("identifier", str(ug)))))
        users = approvers_block.get("users", [])
        if isinstance(users, list):
            for u in users:
                if isinstance(u, str):
                    approvers.append(u)
                elif isinstance(u, dict):
                    approvers.append(str(u.get("name", u.get("email", str(u)))))

    return approvers, message


def _stage_name_from_fqn(base_fqn: str) -> str:
    """Extract a stage name from a baseFqn like 'pipeline.stages.Deploy_Prod.spec...'."""
    if not base_fqn:
        return "Unknown"
    parts = base_fqn.split(".")
    try:
        idx = parts.index("stages")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    except ValueError:
        pass
    return "Unknown"


def _format_approvals(
    execution: PipelineExecution,
    raw_node_map: dict,
) -> str:
    """Format approval status output."""
    approvals = _find_approval_nodes(execution, raw_node_map)

    if not approvals:
        return "No approval steps found in this execution."

    icon = status_icon(execution.status)
    lines = [
        f"## Approval Status: {execution.pipeline_name} (Run #{execution.run_sequence})",
        f"Execution Status: {icon} {execution.status.value}",
        "",
    ]

    pending = [a for a in approvals if a["status"] == ExecutionStatus.APPROVAL_WAITING]
    approved = [a for a in approvals if a["status"] == ExecutionStatus.SUCCESS]
    rejected = [a for a in approvals if a["status"] == ExecutionStatus.APPROVAL_REJECTED]

    if pending:
        lines.append("### Pending Approvals")
        for a in pending:
            duration = format_duration_ms(a["start_ts"], None)  # still waiting
            lines.append(f"🔐 **{a['name']}** (Stage: {a['stage_name']})")
            lines.append(f"   Waiting for: {duration}")
            if a["message"]:
                lines.append(f'   Message: "{a["message"]}"')
            if a["approvers"]:
                lines.append(f"   Approvers: {', '.join(a['approvers'])}")
            lines.append("")

    if approved:
        lines.append("### Completed Approvals")
        for a in approved:
            rel = format_relative_time(a["end_ts"])
            lines.append(f"✅ **{a['name']}** (Stage: {a['stage_name']}) — Approved {rel}")
        lines.append("")

    if rejected:
        lines.append("### Rejected Approvals")
        for a in rejected:
            rel = format_relative_time(a["end_ts"])
            lines.append(f"🚫 **{a['name']}** (Stage: {a['stage_name']}) — Rejected {rel}")
        lines.append("")

    # Anything else (expired, aborted, failed approval steps) must still
    # surface — an expired approval gate is a common "pipeline stuck" cause.
    shown = {ExecutionStatus.APPROVAL_WAITING, ExecutionStatus.SUCCESS, ExecutionStatus.APPROVAL_REJECTED}
    others = [a for a in approvals if a["status"] not in shown]
    if others:
        lines.append("### Other Approval Steps")
        for a in others:
            rel = format_relative_time(a["end_ts"])
            lines.append(
                f"{status_icon(a['status'])} **{a['name']}** (Stage: {a['stage_name']})"
                f" — {a['status'].value} {rel}"
            )
        lines.append("")

    return "\n".join(lines).strip()


# ------------------------------------------------------------------
# CI infrastructure enrichment
# ------------------------------------------------------------------

_CI_INFRA_STEP_TYPES = {"liteEngineTask"}


def _enrich_ci_infra_from_raw_graph(
    execution: PipelineExecution,
    failed_step_tasks: list[tuple[int, int, str, str]],
    detail_map: dict[str, str],
    raw_node_map: dict,
) -> None:
    """Find liteEngineTask nodes for failed/expired stages without step-level detail."""
    covered_stage_ids = {stage_id for _si, _sti, stage_id, _step_id in failed_step_tasks}
    uncovered_stages = [
        s for s in execution.stages
        if s.status in _FAILED_STATUSES and s.stage_id not in covered_stage_ids
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

        base_fqn = raw_node.get("baseFqn", "")
        if not base_fqn:
            continue
        fqn_parts = base_fqn.split(".")
        try:
            stages_idx = fqn_parts.index("stages")
            stage_identifier = fqn_parts[stages_idx + 1] if stages_idx + 1 < len(fqn_parts) else None
        except (ValueError, IndexError):
            continue

        if stage_identifier not in uncovered_identifiers:
            continue

        matching_stage = next(
            (s for s in uncovered_stages if s.identifier == stage_identifier), None
        )
        if not matching_stage:
            continue

        detail_map[f"{matching_stage.stage_id}/liteEngineTask"] = extract_node_details(raw_node)


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def _format_execution(ex: PipelineExecution) -> str:
    """Format a full execution into readable markdown."""
    duration = format_duration_ms(ex.start_ts, ex.end_ts)
    branch_info = f" | Branch: {ex.branch}" if ex.branch else ""
    trigger = f"{ex.trigger_type} by {ex.triggered_by}" if ex.triggered_by else ex.trigger_type

    lines = [
        f"## Execution: {ex.pipeline_name} (Run #{ex.run_sequence})",
        f"Status: {status_label(ex.status)} | Duration: {duration}{branch_info}",
        f"Trigger: {trigger}",
    ]
    if ex.execution_url:
        lines.append(f"Harness: {ex.execution_url}")

    # Stages section
    total = len(ex.stages)
    failed_count = sum(
        1 for s in ex.stages
        if s.status in (ExecutionStatus.FAILED, ExecutionStatus.ERRORED)
    )
    lines.append("")
    header = f"### Stages ({total} total"
    if failed_count > 0:
        header += f", {failed_count} failed"
    header += "):"
    lines.append(header)

    for stage in ex.stages:
        dur = format_duration_ms(stage.start_ts, stage.end_ts)
        icon = status_icon(stage.status)
        marker = ""
        if stage.status in (ExecutionStatus.FAILED, ExecutionStatus.ERRORED):
            marker = "  ← FAILED"
        lines.append(f"  {icon} {stage.name} ({dur}){marker}")

        if stage.child_execution_id:
            lines.append(
                f"     ↳ Child Pipeline execution: `{stage.child_execution_id}`"
                " (follow with hawkeye_get_child_execution or hawkeye_diagnose)"
            )

        if stage.failure_info and stage.failure_info.message:
            ftypes = (
                ", ".join(stage.failure_info.failure_types)
                if stage.failure_info.failure_types
                else "ERROR"
            )
            lines.append(
                f'     └─ Failure: {ftypes} — "{stage.failure_info.message}"'
            )

    return "\n".join(lines)


def _find_stage(execution: PipelineExecution, stage_id: str) -> StageExecution | None:
    """Find a stage by ID or identifier."""
    for stage in execution.stages:
        if stage.stage_id == stage_id or stage.identifier == stage_id:
            return stage
    return None


def _format_stage_detail(execution: PipelineExecution, stage: StageExecution) -> str:
    """Format detailed stage view with all steps."""
    duration = format_duration_ms(stage.start_ts, stage.end_ts)
    lines = [
        f"## Stage: {stage.name}",
        f"Status: {status_label(stage.status)} | Duration: {duration} | Type: {stage.stage_type}",
        f"Pipeline: {execution.pipeline_name} (Run #{execution.run_sequence})",
        "",
    ]

    if stage.failure_info and stage.failure_info.message:
        ftypes = (
            ", ".join(stage.failure_info.failure_types)
            if stage.failure_info.failure_types
            else "ERROR"
        )
        lines.append(f'**Failure:** {ftypes} — "{stage.failure_info.message}"')
        lines.append("")

    if not stage.steps:
        lines.append("No step details available for this stage.")
        return "\n".join(lines)

    lines.append("### Steps:")
    lines.append("| # | Step | Type | Status | Duration | Delegate |")
    lines.append("|---|------|------|--------|----------|----------|")

    for i, step in enumerate(stage.steps, 1):
        dur = format_duration_ms(step.start_ts, step.end_ts)
        icon = status_icon(step.status)
        delegate = step.delegate_info or "—"
        lines.append(
            f"| {i} | {step.name} | {step.step_type}"
            f" | {icon} {step.status.value} | {dur} | {delegate} |"
        )

        if step.failure_info and step.failure_info.message:
            ftypes = (
                ", ".join(step.failure_info.failure_types)
                if step.failure_info.failure_types
                else "ERROR"
            )
            lines.append("")
            lines.append(f'**Step {i} Failure:** {ftypes} — "{step.failure_info.message}"')

    return "\n".join(lines)


def _format_logs(raw_logs: str, execution_id: str, stage_id: str, step_id: str) -> str:
    """Format step logs with truncation."""
    if not raw_logs or not raw_logs.strip():
        return f"No logs available for step `{step_id}` in stage `{stage_id}`."

    log_lines = raw_logs.splitlines()
    total = len(log_lines)
    truncated = False

    if total > _MAX_LOG_LINES:
        log_lines = log_lines[-_MAX_LOG_LINES:]
        truncated = True

    lines = [f"## Step Logs: `{step_id}`"]
    lines.append(f"Execution: `{execution_id}` | Stage: `{stage_id}`")
    if truncated:
        lines.append(f"⚠️ Showing last {_MAX_LOG_LINES} of {total} lines (truncated)")
    else:
        lines.append(f"Lines: {total}")
    lines.append("")
    lines.append("```")
    lines.extend(log_lines)
    lines.append("```")

    return "\n".join(lines)


def _append_console_logs(details: str, raw_logs: str, source: str) -> str:
    """Append truncated console logs to an existing step detail block."""
    logs = truncate_to_tail(raw_logs)
    return (
        f"{details}\n\n"
        f"**Console Logs** ({source}):\n"
        f"```\n{logs}\n```"
    )


def _format_execution_table(
    pipeline_id: str,
    executions: list[PipelineExecution],
    limit: int,
) -> str:
    """Format a table of recent executions."""
    name = executions[0].pipeline_name if executions else pipeline_id
    lines = [
        f"## Recent Executions: {name} (last {limit})",
        "| # | Status | Branch | Trigger | Duration | When |",
        "|---|--------|--------|---------|----------|------|",
    ]

    for ex in executions:
        icon = status_icon(ex.status)
        branch = ex.branch or "—"
        trigger = ex.trigger_type
        dur = format_duration_ms(ex.start_ts, ex.end_ts)
        when = format_relative_time(ex.start_ts)
        lines.append(
            f"| {ex.run_sequence} | {icon} {ex.status.value}"
            f" | {branch} | {trigger} | {dur} | {when} |"
        )

    return "\n".join(lines)


_FAILED_STATUSES = frozenset({ExecutionStatus.FAILED, ExecutionStatus.ERRORED, ExecutionStatus.EXPIRED})
_SKIPPED_STATUSES = frozenset({ExecutionStatus.SKIPPED, ExecutionStatus.NOT_STARTED})


def _format_all_stages(
    execution: PipelineExecution,
    detail_map: dict[str, str],
) -> str:
    """Format all stages with step tables and failed step details."""
    duration = format_duration_ms(execution.start_ts, execution.end_ts)
    lines: list[str] = [
        f"## All Stages: {execution.pipeline_name} (Run #{execution.run_sequence})",
        f"Status: {status_label(execution.status)} | Duration: {duration}",
        "",
    ]

    # Count statuses for summary
    success_count = 0
    failed_count = 0
    skipped_count = 0
    running_count = 0

    # Group consecutive skipped stages for collapsing
    stage_idx = 0
    total_stages = len(execution.stages)

    while stage_idx < total_stages:
        stage = execution.stages[stage_idx]

        # Check for consecutive skipped stages
        if stage.status in _SKIPPED_STATUSES:
            skipped_run: list[StageExecution] = []
            while stage_idx < total_stages and execution.stages[stage_idx].status in _SKIPPED_STATUSES:
                skipped_run.append(execution.stages[stage_idx])
                stage_idx += 1
            skipped_count += len(skipped_run)

            if len(skipped_run) > 3:
                first_num = stage_idx - len(skipped_run) + 1
                last_num = stage_idx
                names = ", ".join(s.name for s in skipped_run)
                lines.append(f"**Stages {first_num}-{last_num}:** ⏭️ Skipped ({len(skipped_run)} stages)")
                lines.append(f"  {names}")
                lines.append("")
            else:
                for s in skipped_run:
                    dur = format_duration_ms(s.start_ts, s.end_ts)
                    lines.append(f"**Stage: {s.name}** — ⏭️ Skipped ({dur}) [{s.stage_type}]")
                    lines.append("")
            continue

        # Non-skipped stage
        stage_num = stage_idx + 1
        dur = format_duration_ms(stage.start_ts, stage.end_ts)
        icon = status_icon(stage.status)

        if stage.status in _FAILED_STATUSES:
            failed_count += 1
        elif stage.status == ExecutionStatus.SUCCESS:
            success_count += 1
        elif stage.status == ExecutionStatus.RUNNING:
            running_count += 1

        lines.append(f"**Stage {stage_num}: {stage.name}** — {icon} {stage.status.value} ({dur}) [{stage.stage_type}]")

        # Pipeline-type indicator (child pipeline)
        if _is_pipeline_step_type(stage.stage_type):
            if stage.child_execution_id:
                lines.append(
                    f"  ↳ Child Pipeline execution: `{stage.child_execution_id}`"
                    " (follow with hawkeye_get_child_execution)"
                )
            else:
                lines.append("  ↳ Child Pipeline (details in child execution)")

        if stage.steps:
            lines.append("")
            lines.append("| Step | Status | Duration | Type |")
            lines.append("|------|--------|----------|------|")

            for step in stage.steps:
                step_dur = format_duration_ms(step.start_ts, step.end_ts)
                step_icon = status_icon(step.status)
                lines.append(f"| {step.name} | {step_icon} {step.status.value} | {step_dur} | {step.step_type} |")

            # Show inline details for failed steps
            for step in stage.steps:
                if step.status in _FAILED_STATUSES:
                    key = f"{stage.stage_id}/{step.step_id}"
                    detail = detail_map.get(key)
                    if detail:
                        lines.append("")
                        lines.append(detail)

        # Show CI infra node details (liteEngineTask) if no steps matched
        infra_key = f"{stage.stage_id}/liteEngineTask"
        infra_detail = detail_map.get(infra_key)
        if infra_detail:
            lines.append("")
            lines.append(infra_detail)

        lines.append("")
        stage_idx += 1

    # Summary line
    parts: list[str] = []
    if success_count:
        parts.append(f"{success_count} succeeded")
    if failed_count:
        parts.append(f"{failed_count} failed")
    if skipped_count:
        parts.append(f"{skipped_count} skipped")
    if running_count:
        parts.append(f"{running_count} running")
    lines.append(f"**Summary:** {' | '.join(parts)}")

    return "\n".join(lines)


def _format_child_execution(
    parent: PipelineExecution,
    parent_stage: StageExecution,
    child: PipelineExecution,
) -> list[str]:
    """Format child pipeline execution summary."""
    parent_dur = format_duration_ms(parent_stage.start_ts, parent_stage.end_ts)
    child_dur = format_duration_ms(child.start_ts, child.end_ts)

    lines: list[str] = [
        f"## Child Pipeline: {child.pipeline_name} (Run #{child.run_sequence})",
        f"Parent: {parent.pipeline_name} (Run #{parent.run_sequence}) → "
        f"Stage: {parent_stage.name} ({parent_dur})",
        f"Status: {status_label(child.status)} | Duration: {child_dur}",
        "",
    ]

    if child.execution_url:
        lines.append(f"Harness: {child.execution_url}")
        lines.append("")

    # Stage summary
    lines.append("### Child Stages:")
    for stage in child.stages:
        dur = format_duration_ms(stage.start_ts, stage.end_ts)
        icon = status_icon(stage.status)
        marker = ""
        if stage.status in _FAILED_STATUSES:
            marker = "  ← FAILED"
        lines.append(f"  {icon} {stage.name} ({dur}){marker}")

        if stage.failure_info and stage.failure_info.message:
            ftypes = (
                ", ".join(stage.failure_info.failure_types)
                if stage.failure_info.failure_types
                else "ERROR"
            )
            lines.append(
                f'     └─ Failure: {ftypes} — "{stage.failure_info.message}"'
            )

    return lines
