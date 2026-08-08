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

from core.formatters import format_log_window
from core.models import (
    FAILED_STATUSES,
    SKIPPED_STATUSES,
    ExecutionStatus,
    PipelineExecution,
    StageExecution,
)
from providers.harness.parser import extract_node_details
from providers.harness.url_patterns import parse_harness_url
from tools._gather import (
    append_console_logs,
    build_step_details,
    enrich_ci_infra,
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
    status_label,
)

logger = logging.getLogger(__name__)

# Backwards-compatible aliases — implementations live in core.models and
# tools/_gather.py
_FAILED_STATUSES = FAILED_STATUSES
_SKIPPED_STATUSES = SKIPPED_STATUSES
_is_pipeline_step_type = is_pipeline_step_type
_append_console_logs = append_console_logs


def register_execution_tools(mcp: FastMCP) -> None:
    """Register all execution tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_step_logs(
        execution_id: str = "",
        stage_id: str = "",
        step_id: str = "",
        url: str = "",
        log_start: int = 0,
        log_lines: int = 100,
    ) -> str:
        """Get detailed step execution information including script source,
        outputs, console logs, and failure details.

        Canonical parameters: execution_id + stage_id + step_id.  The only
        alias form is url= (a Harness UI execution URL; its ?stage= and
        ?step= query params fill stage_id/step_id and take precedence over
        the explicit parameters).  There are no bare `stage`/`step`
        parameters — those spellings are rejected by the schema.

        step_id accepts: a step identifier or display name, a
        group-qualified path for steps inside step groups (e.g.
        `deploy_apim/ContainerStep` — required when two groups reuse the
        same step name), or the runtime node UUID shown by
        hawkeye_get_stage_detail.  An ambiguous step_id returns the list of
        matching candidates instead of guessing.

        Console logs are paginated: by default the LAST log_lines lines are
        returned; pass log_start=<1-based line number> to read a window
        starting there (log_start=1 reads from the beginning).  Truncation
        is always stated explicitly in the output.
        """
        params = {
            "execution_id": execution_id,
            "stage_id": stage_id,
            "step_id": step_id,
            "url": url,
            "log_start": log_start,
            "log_lines": log_lines,
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
                return (
                    "Error: Provide execution_id (canonical form: execution_id"
                    " + stage_id + step_id) or url=<Harness execution URL>."
                )
            if not sid:
                return (
                    "Error: Provide stage_id (canonical form: execution_id + "
                    "stage_id + step_id) or a Harness URL with ?stage=."
                )
            if not stid:
                return (
                    "Error: Provide step_id (canonical form: execution_id + "
                    "stage_id + step_id) or a Harness URL with ?step=."
                )

            provider = await get_provider()
            result = await provider.get_step_logs(eid, sid, stid)
            node_data = result.get("node")
            subgraph_data = result.get("subgraph")

            ambiguous = result.get("ambiguous_matches") or []
            if ambiguous:
                lines = [
                    f"❌ Ambiguous step_id `{stid}` in stage `{sid}` — "
                    f"{len(ambiguous)} steps match. Retry with one of:",
                    "",
                ]
                for c in ambiguous:
                    icon = status_icon(
                        ExecutionStatus.from_str(c.get("status", "Unknown"))
                    )
                    lines.append(
                        f'  • step_id="{c.get("qualified_id") or c.get("identifier")}"'
                        f" — {c.get('name')} ({c.get('step_type')})"
                        f" {icon} {c.get('status')}"
                        f" [node UUID: `{c.get('uuid')}`]"
                    )
                lines.append("")
                lines.append(
                    "Use the group-qualified form `<step_group>/<step>` or the "
                    "node UUID as step_id."
                )
                return "\n".join(lines)

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
                        step_ref = n.get("qualified_id") or n["identifier"]
                        lines.append(
                            f"  • {step_ref} ({n['step_type']}) "
                            f"— {icon} {n['status']}{stage_hint}"
                        )
                return "\n".join(lines)

            # Tier 1: Graph/subgraph data (always available)
            details = extract_node_details(node_data, subgraph_data)

            # Tier 2: Official Harness Log Service API console logs, windowed
            # so any part of a long log is reachable (log_start pages it).
            api_logs = result.get("api_logs")
            if api_logs:
                api_logs = format_log_window(
                    str(api_logs),
                    start_line=max(0, int(log_start)),
                    max_lines=clamp_limit(log_lines, lo=1, hi=1000, default=100),
                )

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

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_recent_executions(
        pipeline_id: str,
        limit: int = 10,
        status: str | None = None,
    ) -> str:
        """List recent executions for a pipeline.

        Summary table with status, branch, trigger, duration.

        Useful for seeing execution history at a glance. Filter by status (e.g. "Failed")
        to find recent failures.

        No filters are sent to Harness unless supplied here; an unrecognised
        status value is rejected client-side with the list of valid values
        (Harness 400s on unknown filter enums).
        """
        params = {"pipeline_id": pipeline_id, "limit": limit, "status": status}

        async def _run() -> str:
            safe_limit = clamp_limit(limit)
            status_filter = None
            if status:
                status_filter = ExecutionStatus.from_str(status)
                if (
                    status_filter is ExecutionStatus.UNKNOWN
                    and status.strip() != ExecutionStatus.UNKNOWN.value
                ):
                    valid = ", ".join(
                        s.value for s in ExecutionStatus if s is not ExecutionStatus.UNKNOWN
                    )
                    return (
                        f"Error: '{status}' is not a valid execution status filter, "
                        f"so it was not sent to Harness. Valid values: {valid}."
                    )
            resolved_id = await resolve_pipeline_id(pipeline_id)

            provider = await get_provider()
            executions = await provider.list_recent_executions(
                resolved_id, limit=safe_limit, status=status_filter
            )
            if not executions:
                msg = f"No executions found for pipeline `{pipeline_id}`"
                if status:
                    msg += f" with status '{status}'"
                return msg + "."

            return _format_execution_table(pipeline_id, executions, safe_limit)

        return await run_tool("hawkeye_list_recent_executions", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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
            failed_step_refs: list[tuple[str, str]] = []
            for stage in execution.stages:
                if stage.status in SKIPPED_STATUSES:
                    continue
                for step in stage.steps:
                    if step.status in FAILED_STATUSES:
                        failed_step_refs.append((stage.stage_id, step.step_id))

            # Fetch details for failed steps in parallel + raw graph for CI
            # infra enrichment
            detail_task = build_step_details(
                provider, eid, failed_step_refs,
                unavailable_note="(details unavailable)",
            )
            raw_graph_task = provider.get_raw_execution_graph(eid)
            results = await asyncio.gather(
                detail_task, raw_graph_task, return_exceptions=True
            )
            detail_map: dict[str, str] = (
                results[0] if isinstance(results[0], dict) else {}
            )
            raw_node_map = results[1] if isinstance(results[1], dict) else None

            # Enrich with CI infra nodes for expired stages that had no failed steps
            if raw_node_map:
                covered = {stage_id for stage_id, _step_id in failed_step_refs}
                enrich_ci_infra(execution, covered, detail_map, raw_node_map)

            return _format_all_stages(execution, detail_map)

        return await run_tool("hawkeye_get_all_stages", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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

            # Get failed step details from child (limited to first 5)
            failed_in_child: list[tuple[str, str]] = []
            for cs in child_execution.stages:
                if cs.status not in FAILED_STATUSES:
                    continue
                for step in cs.steps:
                    if step.status in FAILED_STATUSES:
                        failed_in_child.append((cs.stage_id, step.step_id))

            child_details = await build_step_details(
                provider, child_id, failed_in_child[:5], unavailable_note=""
            )
            for csid, cstid in failed_in_child[:5]:
                detail = child_details.get(f"{csid}/{cstid}")
                if not detail:
                    continue
                lines.append("")
                lines.append(f"**Failed Step Details — {cstid}:**")
                lines.append(detail)

            return "\n".join(lines)

        return await run_tool("hawkeye_get_child_execution", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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
                raw_node = (
                    raw_node_map.get(step.step_id, {})
                    if isinstance(raw_node_map, dict)
                    else {}
                )
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
    shown = {
        ExecutionStatus.APPROVAL_WAITING,
        ExecutionStatus.SUCCESS,
        ExecutionStatus.APPROVAL_REJECTED,
    }
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
    lines.append("| # | Step | Type | Status | Duration | Delegate | Step ID | Node UUID |")
    lines.append("|---|------|------|--------|----------|----------|---------|-----------|")

    for i, step in enumerate(stage.steps, 1):
        dur = format_duration_ms(step.start_ts, step.end_ts)
        icon = status_icon(step.status)
        delegate = step.delegate_info or "—"
        step_ref = (
            f"{step.group_path}/{step.identifier}" if step.group_path else step.identifier
        )
        lines.append(
            f"| {i} | {step.name} | {step.step_type}"
            f" | {icon} {step.status.value} | {dur} | {delegate}"
            f" | `{step_ref}` | `{step.step_id}` |"
        )

        if step.failure_info and step.failure_info.message:
            ftypes = (
                ", ".join(step.failure_info.failure_types)
                if step.failure_info.failure_types
                else "ERROR"
            )
            lines.append("")
            lines.append(f'**Step {i} Failure:** {ftypes} — "{step.failure_info.message}"')

    lines.append("")
    lines.append(
        "Pass `Step ID` (group-qualified: `<step_group>/<step>`) or `Node UUID` "
        "as `step_id` to hawkeye_get_step_logs — bare step names are ambiguous "
        "when step groups reuse the same step name."
    )
    return "\n".join(lines)


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
            while (
                stage_idx < total_stages
                and execution.stages[stage_idx].status in _SKIPPED_STATUSES
            ):
                skipped_run.append(execution.stages[stage_idx])
                stage_idx += 1
            skipped_count += len(skipped_run)

            if len(skipped_run) > 3:
                first_num = stage_idx - len(skipped_run) + 1
                last_num = stage_idx
                names = ", ".join(s.name for s in skipped_run)
                lines.append(
                    f"**Stages {first_num}-{last_num}:** "
                    f"⏭️ Skipped ({len(skipped_run)} stages)"
                )
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

        lines.append(
            f"**Stage {stage_num}: {stage.name}** — {icon} "
            f"{stage.status.value} ({dur}) [{stage.stage_type}]"
        )

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
                lines.append(
                    f"| {step.name} | {step_icon} {step.status.value}"
                    f" | {step_dur} | {step.step_type} |"
                )

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
