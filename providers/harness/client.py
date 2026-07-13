"""Harness NextGen REST API client — read-only.

Uses ``httpx.AsyncClient`` with retry logic, read-only enforcement, and
per-profile authentication via the OS keyring.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import zipfile
from typing import Any

import httpx

from core.credentials import get_token
from core.models import (
    ConnectorInfo,
    DelegateInfo,
    EnvironmentSummary,
    ExecutionGraph,
    ExecutionStatus,
    HarnessProfile,
    InfrastructureSummary,
    InputSet,
    PipelineExecution,
    PipelineSummary,
    SecretInfo,
    ServiceSummary,
    TemplateSummary,
)
from providers.base import PipelineProvider
from providers.harness.parser import (
    parse_environment_list,
    parse_execution_detail,
    parse_execution_graph,
    parse_infrastructure_list,
    parse_input_sets,
    parse_pipeline_list,
    parse_recent_executions,
    parse_secret_item,
    parse_secret_list,
    parse_service_list,
    parse_template_list,
)

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"
_CONNECT_TIMEOUT = 30.0
_READ_TIMEOUT = 60.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_BLOCKED_METHODS = {"PUT", "DELETE", "PATCH"}
_LOG_DOWNLOAD_MAX_POLLS = 6
_LOG_DOWNLOAD_POLL_SECONDS = 2.0


class ReadOnlyViolationError(Exception):
    """Raised when a non-read HTTP method is attempted."""


class HarnessAPIError(Exception):
    """Raised on non-retryable Harness API errors."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Harness API {status_code}: {message}")


def _raw_node(raw_node_map: dict[str, Any], uuid: str) -> dict[str, Any]:
    """Return a shallow copy of a raw graph node, if present."""
    raw = raw_node_map.get(uuid)
    return dict(raw) if isinstance(raw, dict) else {}


def _split_scope_prefix(identifier: str) -> tuple[str, str]:
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


def _normalize_lookup_token(value: str) -> str:
    """Normalize a human or pipeline identifier for fuzzy equality checks."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _tokenize_lookup_value(value: str) -> set[str]:
    """Split an identifier or display name into normalized word tokens."""
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if token}


def _extract_stage_identifier(base_fqn: str) -> str | None:
    """Extract the stage identifier segment from a baseFqn string."""
    parts = base_fqn.split(".") if base_fqn else []
    try:
        index = parts.index("stages")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    return parts[index + 1]


def _stage_matches(
    stage_id: str,
    raw_node: dict[str, Any],
    *,
    base_fqn: str | None,
) -> bool:
    """Return True when a graph node belongs to the requested stage.

    Supports raw execution IDs, pipeline identifiers in ``baseFqn``, and
    human-facing stage names like ``deploy sifi adapter``.
    """
    if not stage_id:
        return True

    if raw_node.get("parentStepId") == stage_id or raw_node.get("setupId") == stage_id:
        return True

    if not base_fqn:
        return False

    stage_lower = stage_id.lower()
    if stage_lower in base_fqn.lower():
        return True

    extracted = _extract_stage_identifier(base_fqn)
    if not extracted:
        return False

    if _normalize_lookup_token(extracted) == _normalize_lookup_token(stage_id):
        return True

    stage_tokens = _tokenize_lookup_value(stage_id)
    extracted_tokens = _tokenize_lookup_value(extracted)
    return bool(stage_tokens) and stage_tokens.issubset(extracted_tokens)


def _candidate_score(node: Any, *, step_id: str, stage_matched: bool, uuid: str) -> int:
    """Score a matched graph node so the best step candidate wins."""
    score = 0
    if uuid == step_id:
        score += 100
    if node.identifier == step_id:
        score += 90
    if node.name.lower() == step_id.lower():
        score += 80
    if stage_matched:
        score += 40
    if node.status in {ExecutionStatus.FAILED, ExecutionStatus.ERRORED, ExecutionStatus.EXPIRED}:
        score += 20
    return score


def _extract_log_key(
    node_data: dict[str, Any] | None,
    subgraph_data: dict[str, Any] | None = None,
) -> str | None:
    """Extract the first exact Harness console log key from graph data."""
    for candidate in _iter_log_key_sources(node_data, subgraph_data):
        executable_responses = candidate.get("executableResponses", [])
        if not isinstance(executable_responses, list):
            continue
        for response in executable_responses:
            if not isinstance(response, dict):
                continue
            async_data = response.get("async", {})
            if not isinstance(async_data, dict):
                continue
            log_keys = async_data.get("logKeys", [])
            if not isinstance(log_keys, list):
                continue
            for log_key in log_keys:
                if isinstance(log_key, str) and log_key.strip():
                    return log_key

    for candidate in _iter_log_key_sources(node_data, subgraph_data):
        log_base_key = candidate.get("logBaseKey")
        if isinstance(log_base_key, str) and log_base_key.strip():
            return log_base_key
    return None


def _iter_log_key_sources(
    node_data: dict[str, Any] | None,
    subgraph_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Collect graph dictionaries that may contain executableResponses."""
    sources: list[dict[str, Any]] = []
    if isinstance(node_data, dict):
        sources.append(node_data)
    if isinstance(subgraph_data, dict):
        sources.append(subgraph_data)
        node_map = subgraph_data.get("nodeMap", {})
        if isinstance(node_map, dict):
            sources.extend(n for n in node_map.values() if isinstance(n, dict))
    return sources


def _extract_logs_from_zip(zip_bytes: bytes) -> str | None:
    """Extract readable log lines from a Harness logs.zip payload."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = sorted(name for name in archive.namelist() if not name.endswith("/"))
            sections: list[str] = []
            for name in names:
                raw = archive.read(name)
                text = raw.decode("utf-8", errors="replace")
                lines = _extract_lines_from_ndjson(text)
                if not lines:
                    continue
                if len(names) > 1:
                    sections.append(f"== {name} ==")
                sections.extend(lines)
            return "\n".join(sections).strip() or None
    except zipfile.BadZipFile:
        logger.debug("Harness Log Service response was not a valid zip file")
        return None


def _extract_lines_from_ndjson(text: str) -> list[str]:
    """Convert Harness NDJSON log content into console-like text lines."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            lines.append(raw_line)
            continue
        message = _extract_log_message(item)
        if message:
            lines.append(message)
    return lines


def _extract_log_message(item: Any) -> str:
    """Extract the human log message from a Harness NDJSON object."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)

    message = _first_string(item, ("message", "msg", "text", "log", "out", "content", "data"))
    nested = item.get("log")
    if not message and isinstance(nested, dict):
        message = _extract_log_message(nested)
    if not message:
        message = json.dumps(item, separators=(",", ":"), default=str)

    ts = _first_string(item, ("timestamp", "time", "timeStamp", "ts"))
    return f"{ts} {message}" if ts and ts not in message else message


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class HarnessClient(PipelineProvider):
    """Async Harness NextGen REST client (read-only)."""

    def __init__(self, profile: HarnessProfile) -> None:
        self._profile = profile
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            token = get_token(self._profile.name)
            if token is None:
                raise RuntimeError(
                    f"No API token found for profile '{self._profile.name}'. "
                    "Run 'make connect' to set up credentials."
                )
            self._client = httpx.AsyncClient(
                base_url=self._profile.base_url,
                headers={
                    "x-api-key": token,
                    "User-Agent": f"Hawkeye/{_VERSION}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    def _scope_params(self, scope: str = "project") -> dict[str, str]:
        """Return the account/org/project query params for the given scope.

        Account-scoped requests must omit org/project identifiers, and
        org-scoped requests must omit the project identifier.
        """
        params = {"accountIdentifier": self._profile.account_id}
        if scope in ("org", "project"):
            params["orgIdentifier"] = self._profile.org_identifier
        if scope == "project":
            params["projectIdentifier"] = self._profile.project_identifier
        return params

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        raw_response: bool = False,
        scope: str = "project",
    ) -> Any:
        """Send an HTTP request with retries and read-only enforcement."""
        method_upper = method.upper()
        if method_upper in _BLOCKED_METHODS:
            raise ReadOnlyViolationError(
                f"HTTP {method_upper} is blocked — Hawkeye is read-only"
            )

        client = await self._ensure_client()
        merged_params = {**self._scope_params(scope), **(params or {})}

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.request(
                    method_upper, path, params=merged_params, json=json_body
                )
                if resp.status_code in _RETRYABLE_STATUSES:
                    last_exc = HarnessAPIError(resp.status_code, resp.text[:200])
                    wait = _BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "Retryable %s on %s (attempt %d/%d), waiting %.1fs",
                        resp.status_code,
                        path,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                if raw_response:
                    return resp.content
                return resp.json()
            except httpx.TimeoutException as exc:
                last_exc = exc
                wait = _BACKOFF_BASE * (2**attempt)
                logger.warning("Timeout on %s (attempt %d/%d)", path, attempt + 1, _MAX_RETRIES)
                await asyncio.sleep(wait)
            except httpx.HTTPStatusError as exc:
                raise HarnessAPIError(exc.response.status_code, exc.response.text[:200]) from exc

        raise last_exc or RuntimeError("Request failed after retries")

    def _unwrap_legacy(self, resp: dict[str, Any]) -> Any:
        """Unwrap ``{"status": "SUCCESS", "data": ...}`` legacy envelope."""
        if isinstance(resp, dict) and resp.get("status") == "SUCCESS" and "data" in resp:
            return resp["data"]
        return resp

    # ------------------------------------------------------------------
    # PipelineProvider implementation
    # ------------------------------------------------------------------

    async def get_execution(self, execution_id: str) -> PipelineExecution:
        """Get full execution details including all stages and steps."""
        raw = await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/v2/{execution_id}",
            params={"renderFullBottomGraph": "true"},
        )
        data = self._unwrap_legacy(raw)
        return parse_execution_detail(
            data,
            base_url=self._profile.base_url,
            account_id=self._profile.account_id,
            org=self._profile.org_identifier,
            project=self._profile.project_identifier,
        )

    async def get_execution_graph(self, execution_id: str) -> ExecutionGraph:
        """Get the execution graph with node hierarchy."""
        raw = await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/getExecutionGraph/{execution_id}",
        )
        data = self._unwrap_legacy(raw)
        return parse_execution_graph(data)

    async def get_raw_execution_graph(self, execution_id: str) -> dict:
        """Get the raw execution graph node map with all fields.

        Unlike ``get_execution_graph``, this returns the raw ``nodeMap`` dict
        preserving fields like ``stepDetails``, ``stepParameters``,
        ``executableResponses``, and ``interruptHistories`` that the parsed
        ``GraphNode`` model does not carry.  Used by the investigation tool
        to enrich CI infrastructure node analysis.
        """
        raw = await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/v2/{execution_id}",
            params={"renderFullBottomGraph": "true"},
        )
        data = self._unwrap_legacy(raw)
        exec_graph = data.get("executionGraph", {})
        return exec_graph.get("nodeMap", {}) if isinstance(exec_graph, dict) else {}

    async def get_step_logs(
        self, execution_id: str, stage_id: str, step_id: str
    ) -> dict:
        """Get step execution details from execution graph and subgraph.

        Returns a dict with graph/subgraph step details and, when available,
        console logs downloaded through the official Harness Log Service API.
        """
        raw_node_map = await self.get_raw_execution_graph(execution_id)
        graph = (
            parse_execution_graph({"nodeMap": raw_node_map})
            if raw_node_map
            else await self.get_execution_graph(execution_id)
        )

        def _build_target(uuid: str, node: Any) -> dict[str, Any]:
            return {
                **_raw_node(raw_node_map, uuid),
                "uuid": node.uuid,
                "name": node.name,
                "identifier": node.identifier,
                "stepType": node.step_type,
                "status": node.status.value,
                "startTs": node.start_ts,
                "endTs": node.end_ts,
                "failureInfo": (
                    {
                        "message": node.failure_info.message,
                        "failureTypeList": node.failure_info.failure_types,
                        "responseMessages": [
                            {"message": m} for m in node.failure_info.response_messages
                        ],
                    }
                    if node.failure_info
                    else None
                ),
                "outcomes": node.outcomes,
                "baseFqn": node.base_fqn,
            }

        # Find the best node matching the step. Some stages contain both a failed
        # deployment step and a later successful rollback step with the same name.
        constrained_matches: list[tuple[int, str, Any]] = []
        unconstrained_matches: list[tuple[int, str, Any]] = []
        for uuid, node in graph.nodes.items():
            if not (
                node.identifier == step_id
                or node.name.lower() == step_id.lower()
                or uuid == step_id
            ):
                continue

            raw_node = _raw_node(raw_node_map, uuid)
            stage_matched = _stage_matches(stage_id, raw_node, base_fqn=node.base_fqn)
            score = _candidate_score(node, step_id=step_id, stage_matched=stage_matched, uuid=uuid)
            match = (score, uuid, node)
            if stage_matched:
                constrained_matches.append(match)
            else:
                unconstrained_matches.append(match)

        target_node: dict[str, Any] | None = None
        target_uuid: str = ""
        matches = constrained_matches or unconstrained_matches
        if matches:
            _score, target_uuid, target = max(matches, key=lambda item: item[0])
            target_node = _build_target(target_uuid, target)

        if target_node is None:
            # Collect available nodes so the caller can suggest valid step IDs
            available: list[dict[str, Any]] = []
            for _uuid, n in graph.nodes.items():
                available.append({
                    "identifier": n.identifier,
                    "name": n.name,
                    "step_type": n.step_type,
                    "status": n.status.value,
                    "base_fqn": n.base_fqn,
                })
            return {"node": None, "subgraph": None, "available_nodes": available}

        # Fetch subgraph for enriched data
        subgraph_data: dict[str, Any] | None = None
        try:
            subgraph_data = await self.get_step_subgraph(execution_id, target_uuid)
        except Exception:
            logger.warning(
                "Subgraph fetch failed for node %s, continuing with graph data",
                target_uuid,
            )

        api_logs = None
        api_log_key = _extract_log_key(target_node, subgraph_data)
        if api_log_key:
            try:
                api_logs = await self._download_logs_for_key(api_log_key)
            except Exception:
                logger.debug(
                    "Log Service API download failed for key %s",
                    api_log_key,
                    exc_info=True,
                )

        return {
            "node": target_node,
            "subgraph": subgraph_data,
            "api_logs": api_logs,
            "api_log_key": api_log_key,
        }

    async def _download_logs_for_key(self, log_key: str) -> str | None:
        """Download console logs for an exact Harness Log Service key.

        The documented endpoint is asynchronous: it returns ``queued`` first,
        then ``success`` with a temporary zip link.  The zip contains NDJSON
        files, one JSON object per line.
        """
        if not log_key:
            return None

        client = await self._ensure_client()
        params = {"accountID": self._profile.account_id, "prefix": log_key}

        download_link = None
        for attempt in range(_LOG_DOWNLOAD_MAX_POLLS):
            resp = await client.post(
                "/gateway/log-service/blob/download",
                params=params,
            )
            if resp.status_code in _RETRYABLE_STATUSES:
                await asyncio.sleep(_LOG_DOWNLOAD_POLL_SECONDS)
                continue
            resp.raise_for_status()
            data = resp.json()
            status = str(data.get("status", "")).lower()
            if status == "success" and data.get("link"):
                download_link = str(data["link"])
                break
            if status == "error":
                logger.debug("Log Service returned error for key %s: %s", log_key, data)
                return None
            if attempt + 1 < _LOG_DOWNLOAD_MAX_POLLS:
                await asyncio.sleep(_LOG_DOWNLOAD_POLL_SECONDS)

        if not download_link:
            logger.debug("Log Service did not produce a download link for key %s", log_key)
            return None

        zip_resp = await client.get(download_link, follow_redirects=True)
        zip_resp.raise_for_status()
        return _extract_logs_from_zip(zip_resp.content)

    async def get_step_subgraph(
        self, execution_id: str, node_execution_id: str
    ) -> dict:
        """Get detailed subgraph for a specific node execution.

        Endpoint: GET /pipeline/api/pipelines/execution/subGraph/{execution_id}/{node_execution_id}
        Params: accountIdentifier, orgIdentifier, projectIdentifier

        Returns detailed node data including:
        - stepParameters (script source, spec, config, environment variables)
        - outcomes (output variables, status)
        - failureInfo (detailed error messages, responseMessages array)
        - unitProgresses (execution progress entries)
        - delegateInfoList (which delegate executed the step)
        - executableResponses (delegate execution responses)
        """
        raw = await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/subGraph/{execution_id}/{node_execution_id}",
        )
        data = self._unwrap_legacy(raw)
        return data if isinstance(data, dict) else {}

    async def list_recent_executions(
        self,
        pipeline_id: str,
        limit: int = 10,
        status: ExecutionStatus | None = None,
    ) -> list[PipelineExecution]:
        """List recent executions for a pipeline."""
        body: dict[str, Any] = {
            "filterType": "PipelineExecution",
            "pipelineIdentifiers": [pipeline_id],
        }
        if status is not None:
            body["status"] = [status.value]
        raw = await self._request(
            "POST",
            "/pipeline/api/pipelines/execution/summary",
            params={"page": "0", "size": str(limit)},
            json_body=body,
        )
        data = self._unwrap_legacy(raw)
        return parse_recent_executions(
            data,
            base_url=self._profile.base_url,
            account_id=self._profile.account_id,
            org=self._profile.org_identifier,
            project=self._profile.project_identifier,
        )

    async def get_pipeline_definition(self, pipeline_id: str, branch: str | None = None) -> dict:  # type: ignore[type-arg]
        """Get pipeline YAML/definition (v1 API)."""
        params: dict[str, str] = {"accountIdentifier": self._profile.account_id}
        if branch:
            params["branch"] = branch
        raw = await self._request(
            "GET",
            f"/v1/orgs/{self._profile.org_identifier}/projects/{self._profile.project_identifier}/pipelines/{pipeline_id}",
            params=params,
        )
        return raw if isinstance(raw, dict) else {"pipeline_yaml": raw}

    async def list_pipelines(
        self, search: str | None = None
    ) -> list[PipelineSummary]:
        """List all pipelines in current project (v1 API)."""
        params: dict[str, str] = {
            "accountIdentifier": self._profile.account_id,
            "limit": "100",
        }
        if search:
            params["search_term"] = search
        raw = await self._request(
            "GET",
            f"/v1/orgs/{self._profile.org_identifier}/projects/{self._profile.project_identifier}/pipelines",
            params=params,
        )
        items = raw if isinstance(raw, list) else raw.get("content", [])
        return parse_pipeline_list(items)

    async def get_input_sets(self, pipeline_id: str) -> list[InputSet]:
        """Get all input sets for a pipeline (v1 API)."""
        raw = await self._request(
            "GET",
            f"/v1/orgs/{self._profile.org_identifier}/projects/{self._profile.project_identifier}/input-sets",
            params={
                "accountIdentifier": self._profile.account_id,
                "pipeline": pipeline_id,
            },
        )
        items = raw if isinstance(raw, list) else raw.get("content", [])
        return parse_input_sets(items)

    async def list_templates(
        self,
        search: str | None = None,
        template_type: str | None = None,
        scope: str = "project",
    ) -> list[TemplateSummary]:
        """List templates in the given scope (template API).

        Returns the last-updated version of each template.  *template_type*
        filters by entity type (Step, Stage, Pipeline, StepGroup, …).
        """
        params: dict[str, str] = {
            "templateListType": "LastUpdated",
            "page": "0",
            "size": "100",
        }
        if search:
            params["searchTerm"] = search
        body: dict[str, Any] = {"filterType": "Template"}
        if template_type:
            body["templateEntityTypes"] = [template_type]
        raw = await self._request(
            "POST",
            "/template/api/templates/list",
            params=params,
            json_body=body,
            scope=scope,
        )
        data = self._unwrap_legacy(raw)
        items = data.get("content", []) if isinstance(data, dict) else data
        return parse_template_list(items if isinstance(items, list) else [])

    async def get_template(
        self,
        template_id: str,
        version_label: str | None = None,
        scope: str = "project",
    ) -> dict:  # type: ignore[type-arg]
        """Get template detail including YAML (template API).

        Omitting *version_label* returns the stable version.  A scope prefix
        in the identifier (``account.`` / ``org.``) overrides *scope*.
        """
        parsed_scope, bare_id = _split_scope_prefix(template_id)
        if parsed_scope != "project":
            scope = parsed_scope

        params: dict[str, str] = {"getDefaultFromOtherRepo": "true"}
        if version_label:
            params["versionLabel"] = version_label
        raw = await self._request(
            "GET",
            f"/template/api/templates/{bare_id}",
            params=params,
            scope=scope,
        )
        data = self._unwrap_legacy(raw)
        return data if isinstance(data, dict) else {"yaml": str(data)}

    async def get_execution_inputs(self, execution_id: str) -> str:
        """Get the input YAML used for a specific execution."""
        raw = await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/{execution_id}/inputsetV2",
            params={"resolveExpressions": "true"},
        )
        data = self._unwrap_legacy(raw)
        if isinstance(data, dict):
            yaml_str = str(data.get("inputSetYaml") or data.get("inputSetTemplateYaml") or "")
            return yaml_str
        return str(data)

    async def list_secrets(
        self, search: str | None = None, scope: str = "project"
    ) -> list[SecretInfo]:
        """List secret metadata in the given scope (v2 secrets API).

        The v2 read API never returns secret values — only metadata such as
        type, secret manager, tags, and modification timestamps.
        """
        params: dict[str, str] = {"pageSize": "100"}
        if search:
            params["searchTerm"] = search
        raw = await self._request("GET", "/ng/api/v2/secrets", params=params, scope=scope)
        data = self._unwrap_legacy(raw)
        items = data.get("content", []) if isinstance(data, dict) else data
        return parse_secret_list(items if isinstance(items, list) else [])

    async def get_secret(self, secret_id: str, scope: str = "project") -> SecretInfo:
        """Get metadata for a single secret (v2 secrets API) — never the value.

        A scope prefix in the identifier (``account.`` / ``org.``) — the form
        used by ``<+secrets.getValue(...)>`` expressions — overrides *scope*.
        """
        parsed_scope, bare_id = _split_scope_prefix(secret_id)
        if parsed_scope != "project":
            scope = parsed_scope
        raw = await self._request("GET", f"/ng/api/v2/secrets/{bare_id}", scope=scope)
        data = self._unwrap_legacy(raw)
        return parse_secret_item(data if isinstance(data, dict) else {})

    async def list_environments(
        self, search: str | None = None
    ) -> list[EnvironmentSummary]:
        """List environments in the current project (environmentsV2 API)."""
        params: dict[str, str] = {"size": "100"}
        if search:
            params["searchTerm"] = search
        raw = await self._request("GET", "/ng/api/environmentsV2", params=params)
        data = self._unwrap_legacy(raw)
        items = data.get("content", []) if isinstance(data, dict) else data
        return parse_environment_list(items if isinstance(items, list) else [])

    async def get_environment(self, environment_id: str) -> dict:  # type: ignore[type-arg]
        """Get environment detail including YAML (environmentsV2 API)."""
        raw = await self._request("GET", f"/ng/api/environmentsV2/{environment_id}")
        data = self._unwrap_legacy(raw)
        if not isinstance(data, dict):
            return {}
        env = data.get("environment", data)
        return env if isinstance(env, dict) else {}

    async def list_infrastructures(
        self, environment_id: str
    ) -> list[InfrastructureSummary]:
        """List infrastructure definitions for an environment."""
        raw = await self._request(
            "GET",
            "/ng/api/infrastructures",
            params={"environmentIdentifier": environment_id, "size": "100"},
        )
        data = self._unwrap_legacy(raw)
        items = data.get("content", []) if isinstance(data, dict) else data
        return parse_infrastructure_list(items if isinstance(items, list) else [])

    async def get_infrastructure(
        self, infrastructure_id: str, environment_id: str
    ) -> dict:  # type: ignore[type-arg]
        """Get infrastructure definition detail including YAML."""
        raw = await self._request(
            "GET",
            f"/ng/api/infrastructures/{infrastructure_id}",
            params={"environmentIdentifier": environment_id},
        )
        data = self._unwrap_legacy(raw)
        if not isinstance(data, dict):
            return {}
        infra = data.get("infrastructure", data)
        return infra if isinstance(infra, dict) else {}

    async def list_services(self, search: str | None = None) -> list[ServiceSummary]:
        """List services in the current project (servicesV2 API)."""
        params: dict[str, str] = {"size": "100"}
        if search:
            params["searchTerm"] = search
        raw = await self._request("GET", "/ng/api/servicesV2", params=params)
        data = self._unwrap_legacy(raw)
        items = data.get("content", []) if isinstance(data, dict) else data
        return parse_service_list(items if isinstance(items, list) else [])

    async def get_service(self, service_id: str) -> dict:  # type: ignore[type-arg]
        """Get service detail including YAML (servicesV2 API)."""
        raw = await self._request("GET", f"/ng/api/servicesV2/{service_id}")
        data = self._unwrap_legacy(raw)
        if not isinstance(data, dict):
            return {}
        service = data.get("service", data)
        return service if isinstance(service, dict) else {}

    async def test_connection(self) -> bool:
        """Verify credentials by listing pipelines (lightweight call)."""
        try:
            await self.list_pipelines()
            return True
        except Exception:
            logger.exception("Connection test failed")
            return False

    async def list_delegates(self) -> list[DelegateInfo]:
        """List all delegate groups with health status."""
        try:
            raw = await self._request(
                "POST",
                "/ng/api/delegate-setup/listDelegates",
            )
        except Exception:
            # Fallback: try GET endpoint
            raw = await self._request(
                "GET",
                "/ng/api/delegate-group/v2",
            )
        data = self._unwrap_legacy(raw)
        # Response is {"resource": [...]} containing delegateGroupDetails
        resource = data if isinstance(data, list) else data.get("resource", [])
        if isinstance(resource, dict):
            # Sometimes wrapped as {"delegateGroupDetails": [...]}
            resource = resource.get("delegateGroupDetails", [resource])
        delegates = []
        for item in resource:
            if not isinstance(item, dict):
                continue
            # Extract tags — modern API returns a flat list of strings
            raw_tags = item.get("tags", [])
            if isinstance(raw_tags, list):
                tags = [str(t) for t in raw_tags]
            elif isinstance(raw_tags, dict):
                # Legacy: groupImplicitSelectors as dict
                tags = []
                for key, values in raw_tags.items():
                    if isinstance(values, list):
                        tags.extend(str(v) for v in values)
                    else:
                        tags.append(f"{key}:{values}")
            else:
                tags = []

            # Also check legacy groupImplicitSelectors if tags is empty
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

            # Modern API uses "delegateReplicas", legacy uses "delegateInstanceDetails".
            # Either key may be present with an explicit null value.
            replicas = (
                item.get("delegateReplicas")
                or item.get("delegateInstanceDetails")
                or []
            )

            # Status from first replica
            if replicas:
                first_status = replicas[0].get("status", "UNKNOWN")
            else:
                first_status = item.get("status", "UNKNOWN")

            # Connected: modern uses "connected", legacy uses "activelyConnected"
            actively_connected = bool(
                item.get("connected", item.get("activelyConnected", False))
            )

            delegates.append(DelegateInfo(
                identifier=str(item.get("delegateGroupIdentifier", item.get("identifier", item.get("name", "")))),
                name=str(item.get("groupName", item.get("name", ""))),
                status=str(first_status),
                actively_connected=actively_connected,
                last_heartbeat=item.get("lastHeartBeat"),
                delegate_type=str(item.get("type", item.get("delegateType", ""))),
                tags=tags,
                instances_connected=len(replicas),
            ))
        return delegates

    async def list_connectors(self, search: str | None = None) -> list[ConnectorInfo]:
        """List connectors with their last connectivity test status."""
        params: dict[str, str] = {"pageSize": "100"}
        if search:
            params["searchTerm"] = search
        raw = await self._request("GET", "/ng/api/connectors", params=params)
        data = self._unwrap_legacy(raw)
        content = data.get("content", []) if isinstance(data, dict) else data
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

            # Determine scope from identifier prefix or connector data
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

    @staticmethod
    def _parse_connector_scope(identifier: str) -> tuple[str, str]:
        """Parse scope prefix from connector identifier.

        Returns (scope, bare_identifier).
        'account.GHConnector' -> ('account', 'GHConnector')
        'org.MyConn'          -> ('org', 'MyConn')
        'GHConnector'         -> ('project', 'GHConnector')
        """
        return _split_scope_prefix(identifier)

    async def test_connector(self, identifier: str, scope: str = "project") -> dict:
        """Test connectivity of a specific connector (read-only POST).

        Uses the client directly to control scope params precisely —
        account-scoped connectors must omit org/project params.
        """
        parsed_scope, bare_id = self._parse_connector_scope(identifier)
        if parsed_scope != "project":
            scope = parsed_scope

        params: dict[str, str] = {
            "accountIdentifier": self._profile.account_id,
        }
        if scope in ("org", "project"):
            params["orgIdentifier"] = self._profile.org_identifier
        if scope == "project":
            params["projectIdentifier"] = self._profile.project_identifier

        client = await self._ensure_client()
        resp = await client.post(
            f"/ng/api/connectors/testConnection/{bare_id}",
            params=params,
        )
        resp.raise_for_status()
        raw = resp.json()
        data = self._unwrap_legacy(raw)
        return data if isinstance(data, dict) else {"status": "UNKNOWN"}
