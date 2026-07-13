"""Response parsing utilities for Harness API responses.

Transforms raw JSON from the Harness NextGen API into Hawkeye Pydantic models.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.formatters import format_status_icon
from core.formatters import truncate_to_tail as truncate_to_tail  # noqa: PLC0414 — re-export
from core.models import (
    ConnectorInfo,
    DelegateInfo,
    EnvironmentSummary,
    ExecutionGraph,
    ExecutionStatus,
    FailureInfo,
    GraphNode,
    InfrastructureSummary,
    InputSet,
    PipelineExecution,
    PipelineSummary,
    SecretInfo,
    ServiceSummary,
    StageExecution,
    StepExecution,
    TemplateSummary,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Execution detail
# ------------------------------------------------------------------


def parse_execution_detail(
    data: dict[str, Any],
    *,
    base_url: str = "https://app.harness.io",
    account_id: str = "",
    org: str = "",
    project: str = "",
) -> PipelineExecution:
    """Parse the v2 execution detail response into a ``PipelineExecution``."""
    summary = data.get("pipelineExecutionSummary", data)
    pipeline_id = summary.get("pipelineIdentifier", "")

    # Parse layout nodes into stages
    layout = summary.get("layoutNodeMap", {})
    starting_id = summary.get("startingNodeId", "")
    stages = _parse_layout_stages(layout, starting_id, data.get("executionGraph", {}))

    execution_id = summary.get("planExecutionId", "")
    url = build_execution_url(base_url, account_id, org, project, pipeline_id, execution_id)

    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier=pipeline_id,
        pipeline_name=summary.get("name", pipeline_id),
        status=ExecutionStatus.from_str(summary.get("status", "Unknown")),
        start_ts=summary.get("startTs"),
        end_ts=summary.get("endTs"),
        trigger_type=_parse_trigger_type(summary),
        triggered_by=_parse_triggered_by(summary),
        branch=summary.get("gitDetails", {}).get("branch") if summary.get("gitDetails") else None,
        run_sequence=summary.get("runSequence", 0),
        execution_url=url,
        stages=stages,
        execution_input_yaml=summary.get("inputSetYaml"),
        tags=_parse_tags(summary.get("tags", [])),
    )


def _parse_trigger_type(summary: dict[str, Any]) -> str:
    ei = summary.get("executionTriggerInfo", {})
    return str(ei.get("triggerType", "MANUAL"))


def _parse_triggered_by(summary: dict[str, Any]) -> str:
    ei = summary.get("executionTriggerInfo", {})
    tb = ei.get("triggeredBy", {})
    return str(tb.get("identifier", tb.get("uuid", "")))


def _parse_tags(raw_tags: Any) -> dict[str, str]:
    if isinstance(raw_tags, dict):
        return raw_tags
    if isinstance(raw_tags, list):
        result: dict[str, str] = {}
        for tag in raw_tags:
            if isinstance(tag, dict):
                result[tag.get("key", "")] = tag.get("value", "")
            elif isinstance(tag, str) and ":" in tag:
                k, _, v = tag.partition(":")
                result[k] = v
        return result
    return {}


def _parse_layout_stages(
    layout: dict[str, Any],
    starting_id: str,
    exec_graph: dict[str, Any],
) -> list[StageExecution]:
    """Walk layoutNodeMap to build stage list."""
    stages: list[StageExecution] = []
    node_map = exec_graph.get("nodeMap", {})

    visited: set[str] = set()
    queue = [starting_id] if starting_id else list(layout.keys())

    while queue:
        node_id = queue.pop(0)
        if node_id in visited or node_id not in layout:
            continue
        visited.add(node_id)
        node = layout[node_id]

        edge_layout = node.get("edgeLayoutList", {})

        # Parallel group wrapper nodes have currentNodeChildren but no
        # meaningful stage data themselves.  Expand their children instead
        # of emitting the wrapper as a stage.
        children = edge_layout.get("currentNodeChildren") or []
        if children:
            # Enqueue child stages (the actual parallel stages)
            for child_id in children:
                if child_id not in visited:
                    queue.append(child_id)
        else:
            # Leaf / real stage node — emit it
            stage_identifier = node.get("nodeIdentifier", node.get("identifier", ""))
            steps = _extract_steps_for_stage(node_id, node_map, stage_identifier)

            stages.append(
                StageExecution(
                    stage_id=node_id,
                    name=node.get("name", ""),
                    identifier=node.get("nodeIdentifier", node.get("identifier", "")),
                    status=ExecutionStatus.from_str(node.get("status", "Unknown")),
                    start_ts=node.get("startTs"),
                    end_ts=node.get("endTs"),
                    stage_type=node.get("nodeType", node.get("module", "Unknown")),
                    failure_info=_parse_failure_info(node.get("failureInfo")),
                    steps=steps,
                    child_execution_id=_parse_child_execution_id(node),
                )
            )

        # Follow next IDs (sequential successors)
        for next_id in edge_layout.get("nextIds", []):
            if next_id not in visited:
                queue.append(next_id)

    return stages


def _extract_steps_for_stage(
    stage_id: str, node_map: dict[str, Any], stage_identifier: str = "",
) -> list[StepExecution]:
    """Extract steps belonging to a stage from the execution graph.

    Matches nodes by:
    1. ``parentStepId`` or ``setupId`` matching *stage_id* (UUID).
    2. ``baseFqn`` containing *stage_id* (UUID).
    3. ``baseFqn`` containing *stage_identifier* in the ``pipeline.stages.<ident>``
       segment — this is the primary match path for CI pipelines where
       ``baseFqn`` uses identifiers, not UUIDs.
    """
    steps: list[StepExecution] = []
    # Pre-compute the baseFqn prefix that a step in this stage would have.
    # Pattern: "pipeline.stages.<identifier>."  (the trailing dot prevents
    # false matches against identifiers that share a prefix).
    fqn_prefix = f"pipeline.stages.{stage_identifier}." if stage_identifier else ""

    for node_id, node in node_map.items():
        # A step belongs to this stage if its baseFqn contains the stage or
        # if the parentStepId matches.  Simplified: look for group=STEP.
        group = node.get("nodeGroup", node.get("stepCategory", ""))
        if group == "STEP" or node.get("stepType") not in (None, "", "STAGE"):
            parent = node.get("parentStepId", node.get("setupId", ""))
            base_fqn = node.get("baseFqn", "")
            # Match by UUID in parentStepId, UUID in baseFqn, or identifier in baseFqn
            if (
                parent == stage_id
                or stage_id in base_fqn
                or (fqn_prefix and fqn_prefix in base_fqn)
            ):
                steps.append(_to_step(node_id, node))
    return steps


def _to_step(node_id: str, node: dict[str, Any]) -> StepExecution:
    return StepExecution(
        step_id=node_id,
        name=node.get("name", ""),
        identifier=node.get("identifier", ""),
        status=ExecutionStatus.from_str(node.get("status", "Unknown")),
        step_type=node.get("stepType", "Unknown"),
        start_ts=node.get("startTs"),
        end_ts=node.get("endTs"),
        failure_info=_parse_failure_info(node.get("failureInfo")),
        delegate_info=_parse_delegate_info(node.get("delegateInfoList")),
        outcomes=node.get("outcomes"),
    )


def _parse_child_execution_id(node: dict[str, Any]) -> str | None:
    """Extract a chained pipeline's child execution ID from a layout node.

    Pipeline-type (chained) stage nodes carry
    ``stepDetails.childPipelineExecutionDetails.planExecutionId`` — the
    authoritative reference to the child execution.
    """
    step_details = node.get("stepDetails")
    if not isinstance(step_details, dict):
        return None
    child_details = step_details.get("childPipelineExecutionDetails")
    if not isinstance(child_details, dict):
        return None
    child_id = child_details.get("planExecutionId")
    return child_id if isinstance(child_id, str) and child_id else None


def _parse_failure_info(raw: Any) -> FailureInfo | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return FailureInfo(
            message=str(raw.get("message", raw.get("failureErrorMessage", ""))),
            failure_types=list(raw.get("failureTypeList") or raw.get("failureTypes") or []),
            response_messages=[
                m.get("message", str(m))
                for m in raw.get("responseMessages", [])
                if isinstance(m, dict)
            ],
        )
    return None


def _parse_delegate_info(raw: Any) -> str | None:
    if not raw:
        return None
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return first.get("name", first.get("id", str(first)))
    return None


# ------------------------------------------------------------------
# Execution graph
# ------------------------------------------------------------------


def parse_execution_graph(data: dict[str, Any]) -> ExecutionGraph:
    """Parse ``nodeMap`` into a structured ``ExecutionGraph``."""
    node_map = data.get("nodeMap", data.get("executionGraph", {}).get("nodeMap", {}))
    root_id = data.get("rootNodeId", "")

    nodes: dict[str, GraphNode] = {}
    for uuid, raw_node in node_map.items():
        nodes[uuid] = GraphNode(
            uuid=uuid,
            name=raw_node.get("name", ""),
            identifier=raw_node.get("identifier", ""),
            step_type=raw_node.get("stepType", "Unknown"),
            status=ExecutionStatus.from_str(raw_node.get("status", "Unknown")),
            start_ts=raw_node.get("startTs"),
            end_ts=raw_node.get("endTs"),
            failure_info=_parse_failure_info(raw_node.get("failureInfo")),
            outcomes=raw_node.get("outcomes"),
            base_fqn=raw_node.get("baseFqn"),
        )

    return ExecutionGraph(root_node_id=root_id, nodes=nodes)


# ------------------------------------------------------------------
# Pipeline list (v1 API)
# ------------------------------------------------------------------


def parse_pipeline_list(raw: list[dict[str, Any]]) -> list[PipelineSummary]:
    """Parse v1 pipeline listing into ``PipelineSummary`` objects."""
    result: list[PipelineSummary] = []
    for item in raw:
        last_exec = item.get("recent_execution_info", [{}])
        last_status = None
        last_ts = None
        if last_exec and isinstance(last_exec, list) and last_exec[0]:
            last_status = ExecutionStatus.from_str(last_exec[0].get("status", "Unknown"))
            last_ts = last_exec[0].get("startTs") or last_exec[0].get("start_ts")

        git_details = item.get("git_details", {}) or {}
        result.append(
            PipelineSummary(
                identifier=item.get("identifier", ""),
                name=item.get("name", ""),
                description=item.get("description"),
                modules=item.get("modules", []),
                last_execution_status=last_status,
                last_execution_ts=last_ts,
                store_type=item.get("store_type", git_details.get("store_type", "INLINE")),
                repo_name=git_details.get("repo_name"),
                created=item.get("created", 0),
                updated=item.get("updated", 0),
            )
        )
    return result


# ------------------------------------------------------------------
# Input sets
# ------------------------------------------------------------------


def parse_input_sets(raw: list[dict[str, Any]]) -> list[InputSet]:
    """Parse input set listing into ``InputSet`` objects."""
    return [
        InputSet(
            identifier=item.get("identifier", ""),
            name=item.get("name", ""),
            pipeline_identifier=item.get("pipeline_identifier", item.get("pipelineIdentifier", "")),
            input_set_yaml=item.get("input_set_yaml", item.get("inputSetYaml", "")),
            created=item.get("created"),
            updated=item.get("updated"),
        )
        for item in raw
    ]


# ------------------------------------------------------------------
# Templates (template API)
# ------------------------------------------------------------------


def parse_template_list(raw: list[dict[str, Any]]) -> list[TemplateSummary]:
    """Parse template listing into ``TemplateSummary`` objects."""
    result: list[TemplateSummary] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        scope = str(item.get("templateScope", "") or "").lower()
        if not scope:
            # Infer scope from which identifiers are present
            if item.get("projectIdentifier"):
                scope = "project"
            elif item.get("orgIdentifier"):
                scope = "org"
            else:
                scope = "account"

        raw_tags = item.get("tags")
        tags = (
            {str(k): str(v) for k, v in raw_tags.items()}
            if isinstance(raw_tags, dict)
            else {}
        )

        result.append(
            TemplateSummary(
                identifier=item.get("identifier", ""),
                name=item.get("name", ""),
                version_label=str(item.get("versionLabel", "") or ""),
                template_entity_type=str(item.get("templateEntityType", "") or ""),
                child_type=item.get("childType"),
                scope=scope,
                stable_template=bool(item.get("stableTemplate", False)),
                description=item.get("description"),
                tags=tags,
                store_type=str(item.get("storeType", "INLINE") or "INLINE"),
                last_updated=item.get("lastUpdatedAt"),
            )
        )
    return result


# ------------------------------------------------------------------
# Secrets, environments, infrastructure, services (NG API)
# ------------------------------------------------------------------


def _infer_scope(item: dict[str, Any]) -> str:
    """Infer entity scope from which org/project identifiers are present."""
    if item.get("projectIdentifier"):
        return "project"
    if item.get("orgIdentifier"):
        return "org"
    return "account"


def _parse_tag_dict(raw_tags: Any) -> dict[str, str]:
    return (
        {str(k): str(v) for k, v in raw_tags.items()}
        if isinstance(raw_tags, dict)
        else {}
    )


def parse_secret_item(item: dict[str, Any]) -> SecretInfo:
    """Parse a single v2 secrets API item into ``SecretInfo``.

    The item wraps the entity as ``{"secret": {...}, "createdAt": …,
    "updatedAt": …}``.  Only metadata is parsed — the ``spec.value`` field
    (always null/absent for reads) is intentionally never touched.
    """
    secret = item.get("secret", item)
    if not isinstance(secret, dict):
        secret = {}
    spec = secret.get("spec", {})
    if not isinstance(spec, dict):
        spec = {}

    return SecretInfo(
        identifier=str(secret.get("identifier", "")),
        name=str(secret.get("name", secret.get("identifier", ""))),
        secret_type=str(secret.get("type", "") or ""),
        secret_manager=str(spec.get("secretManagerIdentifier", "") or ""),
        value_type=str(spec.get("valueType", "") or ""),
        scope=_infer_scope(secret),
        description=secret.get("description") or None,
        tags=_parse_tag_dict(secret.get("tags")),
        created=item.get("createdAt"),
        last_modified=item.get("updatedAt"),
        draft=bool(item.get("draft", False)),
    )


def parse_secret_list(raw: list[dict[str, Any]]) -> list[SecretInfo]:
    """Parse the v2 secrets listing into ``SecretInfo`` objects."""
    return [parse_secret_item(item) for item in raw if isinstance(item, dict)]


def parse_environment_list(raw: list[dict[str, Any]]) -> list[EnvironmentSummary]:
    """Parse environmentsV2 listing into ``EnvironmentSummary`` objects."""
    result: list[EnvironmentSummary] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        env = item.get("environment", item)
        if not isinstance(env, dict):
            continue
        result.append(
            EnvironmentSummary(
                identifier=str(env.get("identifier", "")),
                name=str(env.get("name", env.get("identifier", ""))),
                env_type=str(env.get("type", "") or ""),
                scope=_infer_scope(env),
                description=env.get("description") or None,
                tags=_parse_tag_dict(env.get("tags")),
                store_type=str(env.get("storeType", "INLINE") or "INLINE"),
                created=item.get("createdAt"),
                last_modified=item.get("lastModifiedAt"),
            )
        )
    return result


def parse_infrastructure_list(raw: list[dict[str, Any]]) -> list[InfrastructureSummary]:
    """Parse infrastructures listing into ``InfrastructureSummary`` objects."""
    result: list[InfrastructureSummary] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        infra = item.get("infrastructure", item)
        if not isinstance(infra, dict):
            continue
        result.append(
            InfrastructureSummary(
                identifier=str(infra.get("identifier", "")),
                name=str(infra.get("name", infra.get("identifier", ""))),
                environment_ref=str(infra.get("environmentRef", "") or ""),
                infra_type=str(infra.get("type", "") or ""),
                deployment_type=str(infra.get("deploymentType", "") or ""),
                description=infra.get("description") or None,
                created=item.get("createdAt"),
                last_modified=item.get("lastModifiedAt"),
            )
        )
    return result


def parse_service_list(raw: list[dict[str, Any]]) -> list[ServiceSummary]:
    """Parse servicesV2 listing into ``ServiceSummary`` objects."""
    result: list[ServiceSummary] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        service = item.get("service", item)
        if not isinstance(service, dict):
            continue
        result.append(
            ServiceSummary(
                identifier=str(service.get("identifier", "")),
                name=str(service.get("name", service.get("identifier", ""))),
                description=service.get("description") or None,
                deployment_type=str(service.get("deploymentType", "") or ""),
                scope=_infer_scope(service),
                tags=_parse_tag_dict(service.get("tags")),
                store_type=str(service.get("storeType", "INLINE") or "INLINE"),
                created=item.get("createdAt"),
                last_modified=item.get("lastModifiedAt"),
            )
        )
    return result


# ------------------------------------------------------------------
# Delegates and connectors
# ------------------------------------------------------------------


def _parse_delegate_tags(item: dict[str, Any]) -> list[str]:
    """Extract selector tags — modern flat list or legacy selector dict."""
    raw_tags = item.get("tags", [])
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags]
    elif isinstance(raw_tags, dict):
        tags = []
        for key, values in raw_tags.items():
            if isinstance(values, list):
                tags.extend(str(v) for v in values)
            else:
                tags.append(f"{key}:{values}")
    else:
        tags = []

    if not tags:
        selectors = item.get("groupImplicitSelectors", {})
        if isinstance(selectors, dict):
            for key, values in selectors.items():
                if isinstance(values, list):
                    tags.extend(str(v) for v in values)
                else:
                    tags.append(f"{key}:{values}")
        elif isinstance(selectors, list):
            tags = [str(s) for s in selectors]
    return tags


def parse_delegate_list(raw: Any) -> list[DelegateInfo]:
    """Parse the delegate-group listing into ``DelegateInfo`` objects.

    Accepts either the modern ``{"resource": [...]}`` shape or a bare list;
    handles legacy field names throughout.
    """
    resource = raw if isinstance(raw, list) else raw.get("resource", [])
    if isinstance(resource, dict):
        # Sometimes wrapped as {"delegateGroupDetails": [...]}
        resource = resource.get("delegateGroupDetails", [resource])

    delegates: list[DelegateInfo] = []
    for item in resource:
        if not isinstance(item, dict):
            continue

        tags = _parse_delegate_tags(item)

        # Modern API uses "delegateReplicas", legacy uses
        # "delegateInstanceDetails".  Either key may be explicitly null.
        replicas = (
            item.get("delegateReplicas")
            or item.get("delegateInstanceDetails")
            or []
        )
        if replicas:
            first_status = replicas[0].get("status", "UNKNOWN")
        else:
            first_status = item.get("status", "UNKNOWN")

        actively_connected = bool(
            item.get("connected", item.get("activelyConnected", False))
        )

        delegates.append(DelegateInfo(
            identifier=str(
                item.get(
                    "delegateGroupIdentifier",
                    item.get("identifier", item.get("name", "")),
                )
            ),
            name=str(item.get("groupName", item.get("name", ""))),
            status=str(first_status),
            actively_connected=actively_connected,
            last_heartbeat=item.get("lastHeartBeat"),
            delegate_type=str(item.get("type", item.get("delegateType", ""))),
            tags=tags,
            instances_connected=len(replicas),
        ))
    return delegates


def parse_connector_list(raw: Any) -> list[ConnectorInfo]:
    """Parse the connectors listing into ``ConnectorInfo`` objects."""
    content = raw.get("content", []) if isinstance(raw, dict) else raw
    connectors: list[ConnectorInfo] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        connector = item.get("connector", item)
        status_obj = item.get("status", {})
        if not isinstance(status_obj, dict):
            status_obj = {}
        test_status = str(status_obj.get("status", "UNKNOWN"))
        tested_at = status_obj.get("testedAt")
        error_summary = str(status_obj.get("errorSummary", "") or "")
        delegate_id = str(status_obj.get("delegateId", "") or "")

        # Determine scope from which identifiers are present
        scope = "project"
        identifier = str(connector.get("identifier", ""))
        org_ref = connector.get("orgIdentifier")
        proj_ref = connector.get("projectIdentifier")
        if not org_ref and not proj_ref:
            scope = "account"
        elif org_ref and not proj_ref:
            scope = "org"

        connectors.append(ConnectorInfo(
            identifier=identifier,
            name=str(connector.get("name", identifier)),
            connector_type=str(connector.get("type", "")),
            status=test_status,
            last_tested=tested_at,
            scope=scope,
            delegate_used=delegate_id,
            error_summary=error_summary,
        ))
    return connectors


# ------------------------------------------------------------------
# Node detail extraction (replaces log blob download)
# ------------------------------------------------------------------

# Secret key patterns — values for these keys get masked
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|key|api_key|apikey|credential|auth|bearer|connection_string|sas)",
    re.IGNORECASE,
)

# Pattern for export VAR=VALUE in scripts
_EXPORT_RE = re.compile(r"(export\s+)(\w+)(=)(.*)", re.IGNORECASE)

# CD K8s deployment step types (lowercased) that get failure analysis
_K8S_DEPLOY_STEP_TYPES = (
    "k8srollingdeploy",
    "k8scanarydeploy",
    "k8sbluegreendeploystep",
    "k8sapply",
    "k8srolling",
)


def _mask_secret_value(value: str) -> str:
    """Mask a secret value, showing first 4 chars."""
    if not value or len(value) <= 4:
        return "****"
    return value[:4] + "****"


def _mask_secrets_in_dict(data: dict[str, Any]) -> dict[str, str]:
    """Mask values where key matches secret patterns."""
    result: dict[str, str] = {}
    for k, v in data.items():
        str_val = str(v)
        if _SECRET_KEY_RE.search(k):
            result[k] = _mask_secret_value(str_val)
        else:
            result[k] = str_val
    return result


def _mask_secrets_in_script(script: str) -> str:
    """Mask export VAR=VALUE where VAR matches secret patterns."""
    lines = script.splitlines()
    masked_lines: list[str] = []
    for line in lines:
        m = _EXPORT_RE.match(line.strip())
        if m and _SECRET_KEY_RE.search(m.group(2)):
            masked_lines.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{_mask_secret_value(m.group(4))}")
        else:
            masked_lines.append(line)
    return "\n".join(masked_lines)


def _safe_get(data: dict[str, Any] | None, *keys: str) -> Any:
    """Safely traverse nested dicts."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _format_duration_from_ts(start_ts: Any, end_ts: Any) -> str:
    """Format duration from possibly-string timestamps (raw API data)."""
    from core.formatters import format_duration

    if start_ts is None:
        return "—"
    try:
        start = int(start_ts)
        end = int(end_ts) if end_ts is not None else None
    except (TypeError, ValueError):
        return "—"
    return format_duration(start, end)


def _status_icon_str(status: str) -> str:
    """Get status icon from a raw status string."""
    return format_status_icon(ExecutionStatus.from_str(status or "Unknown"))


def _extract_script(step_params: dict[str, Any] | None, step_type: str) -> str | None:
    """Extract script source from step parameters based on step type."""
    if not step_params:
        return None

    spec = step_params.get("spec", {})
    if not isinstance(spec, dict):
        return None

    # ShellScript: spec.source.spec.script
    script = _safe_get(spec, "source", "spec", "script")
    if script:
        return str(script)

    # Run (CI): spec.command
    command = spec.get("command")
    if command:
        return str(command)

    # Direct script field
    direct_script = spec.get("script")
    if direct_script:
        return str(direct_script)

    return None


def _extract_config(step_params: dict[str, Any] | None, step_type: str) -> dict[str, str]:
    """Extract relevant config fields for non-script step types."""
    if not step_params:
        return {}

    spec = step_params.get("spec", {})
    if not isinstance(spec, dict):
        return {}

    config: dict[str, str] = {}
    step_type_lower = step_type.lower() if step_type else ""

    if "k8s" in step_type_lower or "rolling" in step_type_lower or "deploy" in step_type_lower:
        for key in ("releaseName", "namespace", "connectorRef", "skipDryRun"):
            if key in spec:
                config[key] = str(spec[key])

    elif "approval" in step_type_lower:
        for key in ("approvers", "approvalMessage", "approverInputs"):
            if key in spec:
                config[key] = str(spec[key])

    elif "http" in step_type_lower:
        for key in ("url", "method", "assertion", "headers"):
            if key in spec:
                config[key] = str(spec[key])

    else:
        # Generic: extract top-level spec keys
        for key, val in spec.items():
            if isinstance(val, (str, int, float, bool)):
                config[key] = str(val)

    return config


def extract_node_details(
    node_data: dict[str, Any],
    subgraph_data: dict[str, Any] | None = None,
) -> str:
    """Extract comprehensive step execution details from graph node
    data and optional subgraph enrichment.

    This is Hawkeye's primary method for getting step-level information.
    """
    if not node_data:
        return "No step data available."

    name = node_data.get("name", "unknown")
    step_type = node_data.get("stepType", "Unknown")
    status = node_data.get("status", "Unknown")
    start_ts = node_data.get("startTs")
    end_ts = node_data.get("endTs")
    duration = _format_duration_from_ts(start_ts, end_ts)
    icon = _status_icon_str(status)

    lines: list[str] = []
    lines.append(f"Step: {name} ({step_type}) — {icon} {status} ({duration})")

    # Delegate info
    delegate_list = node_data.get("delegateInfoList")
    if not delegate_list and subgraph_data:
        delegate_list = subgraph_data.get("delegateInfoList")
    if delegate_list and isinstance(delegate_list, list) and delegate_list:
        first = delegate_list[0]
        if isinstance(first, dict):
            delegate_name = first.get("name", first.get("id", str(first)))
        else:
            delegate_name = str(first)
        lines.append(f"Delegate: {delegate_name}")

    # Get step parameters — prefer subgraph (richer data)
    step_params = None
    if subgraph_data:
        # Subgraph may have nodeMap with enriched data
        sg_node_map = subgraph_data.get("nodeMap", {})
        if sg_node_map and isinstance(sg_node_map, dict):
            for _nid, sg_node in sg_node_map.items():
                if isinstance(sg_node, dict) and sg_node.get("stepParameters"):
                    step_params = sg_node.get("stepParameters")
                    break
        if not step_params:
            step_params = subgraph_data.get("stepParameters")
    if not step_params:
        step_params = node_data.get("stepParameters")

    # Script source
    script = _extract_script(step_params, step_type)
    if script:
        masked_script = _mask_secrets_in_script(script)
        lines.append("Executed:")
        lines.append(f"```\n{masked_script}\n```")

    # Non-script step config
    if not script:
        config = _extract_config(step_params, step_type)
        if config:
            masked_config = _mask_secrets_in_dict(config)
            lines.append("Config:")
            for k, v in masked_config.items():
                lines.append(f"  {k} = {v}")

    # Failure details
    failure_info = node_data.get("failureInfo")
    # Prefer subgraph failureInfo if richer
    if subgraph_data:
        sg_failure = subgraph_data.get("failureInfo")
        if sg_failure and isinstance(sg_failure, dict):
            sg_msgs = sg_failure.get("responseMessages", [])
            orig_msgs = (
                failure_info.get("responseMessages", [])
                if isinstance(failure_info, dict)
                else []
            )
            if len(sg_msgs) > len(orig_msgs):
                failure_info = sg_failure

    if failure_info and isinstance(failure_info, dict):
        msg = failure_info.get("message", failure_info.get("failureErrorMessage", ""))
        ftypes = failure_info.get("failureTypeList", failure_info.get("failureTypes", []))
        resp_msgs = failure_info.get("responseMessages", [])

        if msg or ftypes or resp_msgs:
            lines.append("Error:")
            if ftypes:
                lines.append(f"  Type: {', '.join(str(ft) for ft in ftypes)}")
            if msg:
                lines.append(f"  Message: {msg}")
            if resp_msgs:
                lines.append("  Response Details:")
                for rm in resp_msgs:
                    rm_text = rm.get("message", str(rm)) if isinstance(rm, dict) else str(rm)
                    lines.append(f'    "{rm_text}"')

    # Output variables
    outcomes = node_data.get("outcomes")
    if subgraph_data:
        sg_outcomes = subgraph_data.get("outcomes")
        # Prefer subgraph if it has richer outcome data
        if (
            sg_outcomes
            and isinstance(sg_outcomes, dict)
            and (not outcomes or len(str(sg_outcomes)) > len(str(outcomes)))
        ):
            outcomes = sg_outcomes

    if outcomes and isinstance(outcomes, dict):
        output_vars = _safe_get(outcomes, "output", "outputVariables")
        if output_vars and isinstance(output_vars, dict):
            masked_outputs = _mask_secrets_in_dict(output_vars)
            lines.append("Outputs:")
            for k, v in masked_outputs.items():
                lines.append(f"  {k} = {v}")

    # Environment variables
    if step_params and isinstance(step_params, dict):
        spec = step_params.get("spec", {})
        if isinstance(spec, dict):
            env_vars = spec.get("environmentVariables")
            if env_vars and isinstance(env_vars, dict):
                masked_env = _mask_secrets_in_dict(env_vars)
                lines.append("Environment:")
                for k, v in masked_env.items():
                    lines.append(f"  {k} = {v}")

    # CI infrastructure analysis — liteEngineTask is the "Initialize" step
    # that creates the CI pod. When it expires, it's usually a K8s scheduling issue.
    step_type_lower = step_type.lower() if step_type else ""
    status_lower = status.lower() if status else ""
    if step_type_lower == "liteenginetask":
        # Surface task details (INITIALIZATION_PHASE)
        step_details = node_data.get("stepDetails", {})
        if step_details and isinstance(step_details, dict):
            for info_key in ("initStepV2DelegateTaskInfo", "genericStepV1DelegateTaskInfo"):
                task_info = step_details.get(info_key, {})
                if task_info and isinstance(task_info, dict):
                    task_name = task_info.get("taskName", "")
                    if task_name:
                        lines.append(f"Task Phase: {task_name}")
                    break

        # Surface timeout config from stepParameters
        if step_params and isinstance(step_params, dict):
            timeout_str = step_params.get("timeout", "")
            if timeout_str:
                lines.append(f"Timeout Config: {timeout_str}")

        # CI pod initialization failure analysis
        if status_lower in ("expired", "failed", "errored"):
            lines.append("")
            lines.append("⚠️ CI Infrastructure Issue — Pod Initialization Failed")
            lines.append(
                "The liteEngineTask (Initialize step) expired while setting up "
                "the CI build pod. This typically indicates:"
            )
            lines.append("  • K8s node scheduling failure (insufficient CPU/memory, node taints)")
            lines.append("  • Max node group size reached (cluster autoscaler cannot scale up)")
            lines.append("  • Spot node availability issues")
            lines.append("  • Network or image pull issues on the delegate")
            lines.append("")
            lines.append(
                "Note: Detailed scheduling errors are in the step's console logs, "
                "which are only accessible via the Harness UI."
            )

    # CD K8s deployment step analysis
    if step_type_lower in _K8S_DEPLOY_STEP_TYPES and status_lower in (
        "failed", "errored", "expired",
    ):
        lines.append("")
        lines.append("⚠️ CD Infrastructure Issue — K8s Deployment Failed")

        # Extract K8s event details from outcomes
        _outcomes = outcomes
        if not _outcomes:
            _outcomes = node_data.get("outcomes", {})
        if _outcomes and isinstance(_outcomes, dict):
            # Try multiple known paths for K8s pod events
            for events_key in ("k8sEventLogs", "podEvents", "events"):
                event_data = _outcomes.get(events_key)
                if event_data:
                    lines.append("K8s Pod Events:")
                    lines.append(f"```\n{str(event_data)[:2000]}\n```")
                    break

        # Extract from stepDetails for K8s task info
        step_details_data = node_data.get("stepDetails", {})
        if step_details_data and isinstance(step_details_data, dict):
            for info_key in ("k8sTaskDetails", "k8sTaskInfo", "taskDetails"):
                k8s_info = step_details_data.get(info_key, {})
                if k8s_info and isinstance(k8s_info, dict):
                    for events_key in (
                        "k8sClusterEventLogs", "clusterEvents", "events", "podEvents",
                    ):
                        event_logs = k8s_info.get(events_key)
                        if event_logs:
                            lines.append("K8s Cluster Events:")
                            lines.append(f"```\n{str(event_logs)[:2000]}\n```")
                            break
                    break

        # Classify the specific K8s failure from the error message
        if failure_info and isinstance(failure_info, dict):
            fail_msg = str(failure_info.get("message", "")).lower()
            resp_msgs = failure_info.get("responseMessages", [])
            all_msgs = fail_msg + " ".join(
                str(rm.get("message", "") if isinstance(rm, dict) else rm).lower()
                for rm in resp_msgs
            )
            if "progress deadline" in all_msgs:
                lines.append(
                    "Diagnosis: Deployment exceeded progress deadline "
                    "— pods failed to reach ready state"
                )
                lines.append(
                    "Check: readiness/liveness probes, image pull, node resources, pod events"
                )
            elif "insufficient" in all_msgs or "unschedulable" in all_msgs:
                lines.append("Diagnosis: K8s scheduling failure — insufficient cluster resources")
                lines.append("Check: node CPU/memory, autoscaler status, node affinity/taints")
            elif "imagepullbackoff" in all_msgs or "image pull" in all_msgs:
                lines.append("Diagnosis: Image pull failure")
                lines.append("Check: image tag exists, registry credentials, pull secrets")
            elif "crashloopbackoff" in all_msgs or "crashloop" in all_msgs:
                lines.append("Diagnosis: Pod crash loop — application failing to start")
                lines.append("Check: application logs, resource limits, config/secrets")

    # Surface log keys so the user knows where to look in the UI
    exec_responses = node_data.get("executableResponses", [])
    if exec_responses and isinstance(exec_responses, list):
        for er in exec_responses:
            if isinstance(er, dict):
                async_data = er.get("async", {})
                log_keys = async_data.get("logKeys", []) if isinstance(async_data, dict) else []
                if log_keys:
                    lines.append(f"Log Key: {log_keys[0]}")
                    break

    return "\n".join(lines)


# ------------------------------------------------------------------
# Child pipeline execution ID extraction
# ------------------------------------------------------------------


def extract_child_execution_id(node_data: dict[str, Any]) -> str | None:
    """Extract child pipeline execution ID from a Pipeline-type node.

    Pipeline-type nodes in the Harness execution graph represent calls to
    child pipelines.  The child execution ID can appear in several locations.
    This function tries them all and returns the first non-None value.

    Returns the child execution ID, or None if not found.
    """
    if not node_data or not isinstance(node_data, dict):
        return None

    # 1. outcomes.output.pipelineExecutionId (direct ID)
    child_id = _safe_get(node_data, "outcomes", "output", "pipelineExecutionId")
    if isinstance(child_id, str) and child_id:
        return child_id

    # 2. outcomes.output.executionUrl → parse execution ID from URL
    exec_url = _safe_get(node_data, "outcomes", "output", "executionUrl")
    if exec_url and isinstance(exec_url, str):
        parsed = _parse_execution_id_from_url(exec_url)
        if parsed:
            return parsed

    # 3. executableResponses[*].child.childExecutionId
    exec_responses = node_data.get("executableResponses", [])
    if isinstance(exec_responses, list):
        for er in exec_responses:
            if not isinstance(er, dict):
                continue
            # child execution reference
            child_data = er.get("child", {})
            if isinstance(child_data, dict):
                cid = child_data.get("childExecutionId")
                if isinstance(cid, str) and cid:
                    return cid
            # async callback data may contain child ID
            async_data = er.get("async", {})
            if isinstance(async_data, dict):
                callback_ids = async_data.get("callbackIds", [])
                if isinstance(callback_ids, list):
                    for cb_id in callback_ids:
                        if isinstance(cb_id, str) and len(cb_id) > 10:
                            return cb_id

    # 4. stepParameters.spec.* — may contain child pipeline execution ref
    child_id_from_params = _safe_get(
        node_data, "stepParameters", "spec", "pipelineExecutionId"
    )
    if isinstance(child_id_from_params, str) and child_id_from_params:
        return child_id_from_params

    return None


def _parse_execution_id_from_url(url: str) -> str | None:
    """Extract execution ID from a Harness execution URL."""
    # Pattern: .../executions/{execution_id}/pipeline
    match = re.search(r"/executions/([^/?]+)", url)
    return match.group(1) if match else None


# ------------------------------------------------------------------
# Recent executions (summary endpoint)
# ------------------------------------------------------------------


def parse_recent_executions(
    data: dict[str, Any] | list[Any],
    *,
    base_url: str = "https://app.harness.io",
    account_id: str = "",
    org: str = "",
    project: str = "",
) -> list[PipelineExecution]:
    """Parse execution summary list response."""
    raw_items: Any = (
        data.get("content", data.get("executions", []))
        if isinstance(data, dict)
        else data
    )
    items: list[Any] = list(raw_items) if raw_items else []

    results: list[PipelineExecution] = []
    for item in items:
        summary = item.get("pipelineExecutionSummary", item)
        pipeline_id = summary.get("pipelineIdentifier", "")
        execution_id = summary.get("planExecutionId", "")
        url = build_execution_url(base_url, account_id, org, project, pipeline_id, execution_id)

        # Summary items carry layoutNodeMap (no executionGraph), so stages
        # parse with names/statuses but empty step lists.  History-based
        # analysis (failure hotspots, same-stage detection) depends on this.
        layout = summary.get("layoutNodeMap", {})
        stages = (
            _parse_layout_stages(layout, summary.get("startingNodeId", ""), {})
            if isinstance(layout, dict) and layout
            else []
        )

        results.append(
            PipelineExecution(
                plan_execution_id=execution_id,
                pipeline_identifier=pipeline_id,
                pipeline_name=summary.get("name", pipeline_id),
                status=ExecutionStatus.from_str(summary.get("status", "Unknown")),
                start_ts=summary.get("startTs"),
                end_ts=summary.get("endTs"),
                trigger_type=_parse_trigger_type(summary),
                triggered_by=_parse_triggered_by(summary),
                branch=(
                summary.get("gitDetails", {}).get("branch")
                if summary.get("gitDetails")
                else None
            ),
                run_sequence=summary.get("runSequence", 0),
                execution_url=url,
                stages=stages,
                execution_input_yaml=None,
                tags=_parse_tags(summary.get("tags", [])),
            )
        )
    return results


# ------------------------------------------------------------------
# URL builder
# ------------------------------------------------------------------


def build_execution_url(
    base_url: str,
    account_id: str,
    org: str,
    project: str,
    pipeline_id: str,
    execution_id: str,
    module: str = "cd",
) -> str:
    """Build a deep-link URL to a Harness pipeline execution."""
    if not all([base_url, account_id, org, project, pipeline_id, execution_id]):
        return ""
    return (
        f"{base_url.rstrip('/')}/ng/account/{account_id}/module/{module}"
        f"/orgs/{org}/projects/{project}"
        f"/pipelines/{pipeline_id}/executions/{execution_id}/pipeline"
    )


# ------------------------------------------------------------------
# Scope prefixes
# ------------------------------------------------------------------


def split_scope_prefix(identifier: str) -> tuple[str, str]:
    """Split a scope prefix from an entity identifier.

    Returns (scope, bare_identifier).
    'account.MyTemplate' -> ('account', 'MyTemplate')
    'org.MyTemplate'     -> ('org', 'MyTemplate')
    'MyTemplate'         -> ('project', 'MyTemplate')
    """
    if identifier.startswith("account."):
        return "account", identifier[len("account."):]
    if identifier.startswith("org."):
        return "org", identifier[len("org."):]
    return "project", identifier


# NOTE: truncate_to_tail is imported from core.formatters at the top of this
# module and intentionally re-exported here for existing importers.
