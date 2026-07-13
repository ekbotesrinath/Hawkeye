"""Health tools — project-wide operational overview.

2 tools:
- hawkeye_health_overview — the "what's on fire this morning" call:
  disconnected delegates, failing connectors, last-24h pipeline failures,
  executions waiting on humans, and currently-running executions.
- hawkeye_deployment_map — service × environment matrix built from recent
  successful CD executions ("what's deployed where").
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from core.models import (
    FAILED_STATUSES,
    WAITING_ON_HUMAN_STATUSES,
    ConnectorInfo,
    DelegateInfo,
    ExecutionStatus,
    PipelineExecution,
)
from tools._helpers import (
    READ_ONLY_ANNOTATIONS,
    format_relative_time,
    get_provider,
    run_tool,
    status_icon,
)

_DAY_MS = 24 * 60 * 60 * 1000
_OVERVIEW_EXECUTION_WINDOW = 100  # newest project executions examined


def register_health_tools(mcp: FastMCP) -> None:
    """Register all health tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_health_overview() -> str:
        """One-call project health check — "what's on fire this morning?".

        Gathers in parallel: delegate connectivity, connector test status,
        pipeline failures in the last 24 hours, executions waiting on human
        action (approvals/interventions/inputs), and currently running
        executions. Each section degrades gracefully if its API call fails.
        Start here for a morning triage or a scheduled daily digest.
        """
        params: dict[str, Any] = {}

        async def _run() -> str:
            provider = await get_provider()
            delegates_res, connectors_res, executions_res = await asyncio.gather(
                provider.list_delegates(),
                provider.list_connectors(),
                provider.list_project_executions(limit=_OVERVIEW_EXECUTION_WINDOW),
                return_exceptions=True,
            )
            return _format_health_overview(delegates_res, connectors_res, executions_res)

        return await run_tool("hawkeye_health_overview", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_deployment_map() -> str:
        """Show what is deployed where — a service × environment matrix.

        Builds the matrix from the most recent successful CD executions in
        the project (each cell shows the latest deploy's run number and
        age). Use it to answer "which version of X is in prod?" or "when
        did we last deploy to staging?". Coverage is limited to the most
        recent executions window — older deployments may not appear.
        """
        params: dict[str, Any] = {}

        async def _run() -> str:
            provider = await get_provider()
            executions = await provider.list_project_executions(
                limit=_OVERVIEW_EXECUTION_WINDOW
            )
            return _format_deployment_map(executions)

        return await run_tool("hawkeye_deployment_map", params, _run())


# ------------------------------------------------------------------
# Health overview formatting
# ------------------------------------------------------------------


def _format_health_overview(
    delegates_res: list[DelegateInfo] | BaseException,
    connectors_res: list[ConnectorInfo] | BaseException,
    executions_res: list[PipelineExecution] | BaseException,
) -> str:
    now_ms = int(time.time() * 1000)
    lines: list[str] = []
    problem_count = 0
    sections: list[str] = []

    # --- Delegates ---
    if isinstance(delegates_res, BaseException):
        sections.append("### Delegates\n⚠️ Could not fetch delegates: "
                        f"{delegates_res}")
    else:
        disconnected = [d for d in delegates_res if not d.actively_connected]
        connected = len(delegates_res) - len(disconnected)
        if disconnected:
            problem_count += len(disconnected)
            rows = [
                "### Delegates",
                f"{connected}/{len(delegates_res)} connected — "
                f"**{len(disconnected)} DISCONNECTED**:",
            ]
            for d in disconnected:
                heartbeat = format_relative_time(d.last_heartbeat)
                rows.append(
                    f"- 🔴 **{d.name or d.identifier}** — last heartbeat {heartbeat}"
                )
            sections.append("\n".join(rows))
        else:
            sections.append(
                f"### Delegates\n✅ {connected}/{len(delegates_res)} connected"
                if delegates_res
                else "### Delegates\nNo delegate groups visible in this scope."
            )

    # --- Connectors ---
    if isinstance(connectors_res, BaseException):
        sections.append(f"### Connectors\n⚠️ Could not fetch connectors: {connectors_res}")
    else:
        failing = [
            c for c in connectors_res
            if c.status.upper() not in ("SUCCESS", "UNKNOWN", "")
        ]
        untested = [c for c in connectors_res if c.status.upper() in ("UNKNOWN", "")]
        if failing:
            problem_count += len(failing)
            rows = [
                "### Connectors",
                f"**{len(failing)} failing** of {len(connectors_res)}:",
            ]
            for c in failing:
                tested = format_relative_time(c.last_tested)
                error = f" — {c.error_summary}" if c.error_summary else ""
                rows.append(
                    f"- 🔴 **{c.name}** (`{c.identifier}`, {c.connector_type}) "
                    f"last tested {tested}{error}"
                )
            sections.append("\n".join(rows))
        else:
            note = f" ({len(untested)} never tested)" if untested else ""
            sections.append(
                f"### Connectors\n✅ {len(connectors_res) - len(untested)} healthy, "
                f"0 failing{note}"
            )

    # --- Executions: failures last 24h, waiting on humans, running ---
    if isinstance(executions_res, BaseException):
        sections.append(f"### Executions\n⚠️ Could not fetch executions: {executions_res}")
    else:
        failures_24h = [
            ex for ex in executions_res
            if ex.status in FAILED_STATUSES
            and ex.start_ts is not None
            and now_ms - ex.start_ts <= _DAY_MS
        ]
        waiting = [ex for ex in executions_res if ex.status in WAITING_ON_HUMAN_STATUSES]
        running = [ex for ex in executions_res if ex.status == ExecutionStatus.RUNNING]

        if failures_24h:
            problem_count += len(failures_24h)
            rows = [
                "### Failures (last 24 h)",
                f"**{len(failures_24h)} failed execution(s)**:",
                "| Pipeline | Run | Status | When | Failed stage |",
                "|----------|-----|--------|------|--------------|",
            ]
            for ex in failures_24h:
                failed_stage = next(
                    (s.name for s in ex.stages if s.status in FAILED_STATUSES), "—"
                )
                rows.append(
                    f"| {ex.pipeline_name} | #{ex.run_sequence} "
                    f"| {status_icon(ex.status)} {ex.status.value} "
                    f"| {format_relative_time(ex.start_ts)} | {failed_stage} |"
                )
            rows.append(
                "\n💡 Investigate any of these with "
                "`hawkeye_diagnose(execution_id=…)`."
            )
            sections.append("\n".join(rows))
        else:
            sections.append("### Failures (last 24 h)\n✅ No failed executions.")

        if waiting:
            rows = [
                "### Waiting on humans",
                f"**{len(waiting)} execution(s)** need action:",
            ]
            for ex in waiting:
                rows.append(
                    f"- {status_icon(ex.status)} **{ex.pipeline_name}** "
                    f"(Run #{ex.run_sequence}) — {ex.status.value}, "
                    f"waiting since {format_relative_time(ex.start_ts)}"
                    " — details: `hawkeye_check_approvals`"
                )
            sections.append("\n".join(rows))
        else:
            sections.append("### Waiting on humans\n✅ Nothing pending.")

        if running:
            rows = [f"### Currently running ({len(running)})"]
            for ex in running[:10]:
                rows.append(
                    f"- 🔄 **{ex.pipeline_name}** (Run #{ex.run_sequence}) — "
                    f"started {format_relative_time(ex.start_ts)}"
                    " — follow with `hawkeye_watch_execution`"
                )
            if len(running) > 10:
                rows.append(f"- … and {len(running) - 10} more")
            sections.append("\n".join(rows))

    verdict = (
        f"🔴 **{problem_count} issue(s) need attention**"
        if problem_count
        else "✅ **All clear**"
    )
    lines.append("## Hawkeye Health Overview")
    lines.append(verdict)
    lines.append("")
    lines.append("\n\n".join(sections))
    return "\n".join(lines)


# ------------------------------------------------------------------
# Deployment map formatting
# ------------------------------------------------------------------


def _format_deployment_map(executions: list[PipelineExecution]) -> str:
    # Latest SUCCESSFUL deploy per (service, environment)
    latest: dict[tuple[str, str], PipelineExecution] = {}
    cd_seen = 0
    for ex in executions:
        if not ex.service_identifiers or not ex.env_identifiers:
            continue
        cd_seen += 1
        if ex.status != ExecutionStatus.SUCCESS:
            continue
        for svc in ex.service_identifiers:
            for env in ex.env_identifiers:
                key = (svc, env)
                current = latest.get(key)
                if current is None or (ex.start_ts or 0) > (current.start_ts or 0):
                    latest[key] = ex

    if not latest:
        if cd_seen:
            return (
                "No **successful** CD deployments found in the last "
                f"{len(executions)} executions ({cd_seen} CD run(s) seen, "
                "none succeeded). Check failures with `hawkeye_health_overview`."
            )
        return (
            f"No CD deployments found in the last {len(executions)} "
            "executions — this project may only run CI pipelines, or "
            "deployments are older than the window."
        )

    services = sorted({svc for svc, _env in latest})
    envs = sorted({env for _svc, env in latest})

    lines = [
        "## Deployment Map — latest successful deploy per service × environment",
        f"(from the {len(executions)} most recent executions)",
        "",
        "| Service | " + " | ".join(envs) + " |",
        "|---------|" + "|".join("---" for _ in envs) + "|",
    ]
    for svc in services:
        cells = []
        for env in envs:
            cell_ex = latest.get((svc, env))
            if cell_ex is None:
                cells.append("—")
            else:
                cells.append(
                    f"✅ #{cell_ex.run_sequence} · "
                    f"{format_relative_time(cell_ex.start_ts)}"
                )
        lines.append(f"| **{svc}** | " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("### Details")
    for (svc, env), ex in sorted(latest.items()):
        lines.append(
            f"- **{svc}** → **{env}**: {ex.pipeline_name} "
            f"Run #{ex.run_sequence}, {format_relative_time(ex.start_ts)}"
            + (f" — {ex.execution_url}" if ex.execution_url else "")
        )
    return "\n".join(lines)
