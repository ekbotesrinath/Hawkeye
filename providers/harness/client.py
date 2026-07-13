"""Harness NextGen REST API client — read-only.

Uses ``httpx.AsyncClient`` with retry logic, read-only enforcement, and
per-profile authentication via the OS keyring.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from core.cache import TTLCache
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
from providers.base import PipelineProvider, ProviderError
from providers.harness.graph_search import (
    candidate_score,
    extract_log_key,
    extract_logs_from_zip,
    stage_matches,
)
from providers.harness.parser import (
    parse_connector_list,
    parse_delegate_list,
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
    split_scope_prefix,
)

logger = logging.getLogger(__name__)


def _package_version() -> str:
    """Resolve the installed package version (single source: pyproject.toml)."""
    try:
        from importlib.metadata import version

        return version("hawkeye")
    except Exception:  # pragma: no cover - metadata missing in odd installs
        return "0.0.0-dev"


_VERSION = _package_version()
_CONNECT_TIMEOUT = 30.0
_READ_TIMEOUT = 60.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds
_MAX_RETRY_AFTER = 30.0  # cap for server-provided Retry-After
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_BLOCKED_METHODS = {"PUT", "DELETE", "PATCH"}
_MAX_CONCURRENT_REQUESTS = 8
_LOG_DOWNLOAD_MAX_POLLS = 6
_LOG_DOWNLOAD_POLL_SECONDS = 2.0
_MAX_LOG_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB compressed

# Pagination: follow up to this many pages of 100 per listing call
_PAGE_SIZE = 100
_MAX_PAGES = 5

# Response caching (per-client instance, so tests are unaffected)
_LIST_CACHE_TTL = 60  # seconds — listings change rarely within a session
_EXEC_CACHE_TTL = 600  # seconds — completed executions are immutable
_PIPELINE_DEF_CACHE_TTL = 300

# Statuses after which an execution can no longer change
_TERMINAL_STATUSES = frozenset({
    ExecutionStatus.SUCCESS,
    ExecutionStatus.FAILED,
    ExecutionStatus.ERRORED,
    ExecutionStatus.ABORTED,
    ExecutionStatus.EXPIRED,
    ExecutionStatus.APPROVAL_REJECTED,
    ExecutionStatus.ABORTED_BY_FREEZE,
    ExecutionStatus.IGNORE_FAILED,
})

# Read-only enforcement: POST is only permitted for the Harness endpoints
# that are semantically reads (list/summary/test queries).  Everything else
# is rejected, so a future code path cannot accidentally trigger, approve,
# or abort a pipeline.
_ALLOWED_POST_PATHS: tuple[str, ...] = (
    "/pipeline/api/pipelines/execution/summary",
    "/template/api/templates/list",
    "/ng/api/delegate-setup/listDelegates",
    "/ng/api/connectors/testConnection/",
    "/gateway/log-service/blob/download",
)


class ReadOnlyViolationError(Exception):
    """Raised when a non-read HTTP method is attempted."""


def _ensure_post_allowed(path: str) -> None:
    """Raise unless *path* is an allowlisted read-only POST endpoint."""
    for allowed in _ALLOWED_POST_PATHS:
        if path == allowed or (allowed.endswith("/") and path.startswith(allowed)):
            return
    raise ReadOnlyViolationError(
        f"POST to '{path}' is blocked — Hawkeye only allows read-only POST "
        "endpoints (list/summary/test queries)."
    )


def _retry_wait(attempt: int, retry_after: str | None = None) -> float:
    """Compute the backoff delay for a retry attempt.

    Honours a server-provided ``Retry-After`` (seconds form, capped), and
    otherwise applies exponential backoff with jitter.
    """
    if retry_after:
        try:
            return min(float(retry_after), _MAX_RETRY_AFTER)
        except ValueError:
            pass
    # Jitter is for retry spreading, not cryptography
    return _BACKOFF_BASE * (2.0**attempt) + random.uniform(0, 0.5)  # noqa: S311


class HarnessAPIError(ProviderError):
    """Raised on non-retryable Harness API errors."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(status_code, f"Harness API {status_code}: {message}")


def _raw_node(raw_node_map: dict[str, Any], uuid: str) -> dict[str, Any]:
    """Return a shallow copy of a raw graph node, if present."""
    raw = raw_node_map.get(uuid)
    return dict(raw) if isinstance(raw, dict) else {}


# Scope-prefix splitting lives in parser.split_scope_prefix — kept under the
# old private name for existing importers.
_split_scope_prefix = split_scope_prefix


async def _download_capped(url: str) -> bytes | None:
    """Download *url* with a bare HTTP client, capped at a maximum size.

    The signed download link points at external blob storage — it must be
    fetched WITHOUT the Harness API key header, so a dedicated client (no
    default headers) is used instead of the authenticated API client.
    """
    timeout = httpx.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT)
    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=True) as bare_client,
        bare_client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > _MAX_LOG_DOWNLOAD_BYTES:
                logger.warning(
                    "Log download exceeded %d bytes cap, skipping",
                    _MAX_LOG_DOWNLOAD_BYTES,
                )
                return None
            chunks.append(chunk)
    return b"".join(chunks)


class HarnessClient(PipelineProvider):
    """Async Harness NextGen REST client (read-only)."""

    def __init__(self, profile: HarnessProfile) -> None:
        self._profile = profile
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        self._cache = TTLCache(max_size=200)

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
        if method_upper == "POST":
            _ensure_post_allowed(path)

        client = await self._ensure_client()
        merged_params = {**self._scope_params(scope), **(params or {})}

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with self._semaphore:
                    resp = await client.request(
                        method_upper, path, params=merged_params, json=json_body
                    )
                if resp.status_code in _RETRYABLE_STATUSES:
                    last_exc = HarnessAPIError(resp.status_code, resp.text[:200])
                    if attempt + 1 >= _MAX_RETRIES:
                        break
                    wait = _retry_wait(attempt, resp.headers.get("Retry-After"))
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
                if attempt + 1 >= _MAX_RETRIES:
                    break
                logger.warning("Timeout on %s (attempt %d/%d)", path, attempt + 1, _MAX_RETRIES)
                await asyncio.sleep(_retry_wait(attempt))
            except httpx.HTTPStatusError as exc:
                raise HarnessAPIError(exc.response.status_code, exc.response.text[:200]) from exc

        raise last_exc or RuntimeError("Request failed after retries")

    def _unwrap_legacy(self, resp: dict[str, Any]) -> Any:
        """Unwrap ``{"status": "SUCCESS", "data": ...}`` legacy envelope."""
        if isinstance(resp, dict) and resp.get("status") == "SUCCESS" and "data" in resp:
            return resp["data"]
        return resp

    async def _fetch_pages(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        scope: str = "project",
        page_param: str = "page",
        page_size: int = _PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
    ) -> list[Any]:
        """Follow paginated list responses, up to *max_pages* pages.

        Handles both bare-list responses (v1 API) and enveloped
        ``{"content": [...]}`` responses (NG APIs).  Stops early when a
        page comes back short.
        """
        items: list[Any] = []
        for page in range(max_pages):
            merged = {**(params or {}), page_param: str(page)}
            raw = await self._request(
                method, path, params=merged, json_body=json_body, scope=scope
            )
            data = self._unwrap_legacy(raw)
            if isinstance(data, dict):
                batch = data.get("content", [])
            elif isinstance(data, list):
                batch = data
            else:
                break
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < page_size:
                break
        return items

    # ------------------------------------------------------------------
    # PipelineProvider implementation
    # ------------------------------------------------------------------

    async def get_execution(self, execution_id: str) -> PipelineExecution:
        """Get full execution details including all stages and steps.

        Completed (terminal-status) executions are immutable and cached.
        """
        cache_key = f"exec/{execution_id}"
        cached: PipelineExecution | None = self._cache.get(cache_key)
        if cached is not None:
            return cached

        raw = await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/v2/{execution_id}",
            params={"renderFullBottomGraph": "true"},
        )
        data = self._unwrap_legacy(raw)
        execution = parse_execution_detail(
            data,
            base_url=self._profile.base_url,
            account_id=self._profile.account_id,
            org=self._profile.org_identifier,
            project=self._profile.project_identifier,
        )
        if execution.status in _TERMINAL_STATUSES:
            self._cache.set(cache_key, execution, ttl=_EXEC_CACHE_TTL)
        return execution

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
            stage_matched = stage_matches(stage_id, raw_node, base_fqn=node.base_fqn)
            score = candidate_score(node, step_id=step_id, stage_matched=stage_matched, uuid=uuid)
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
        api_log_key = extract_log_key(target_node, subgraph_data)
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
        blob_path = "/gateway/log-service/blob/download"
        _ensure_post_allowed(blob_path)
        params = {"accountID": self._profile.account_id, "prefix": log_key}

        download_link = None
        for attempt in range(_LOG_DOWNLOAD_MAX_POLLS):
            resp = await client.post(blob_path, params=params)
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

        zip_bytes = await _download_capped(download_link)
        if zip_bytes is None:
            return None
        return extract_logs_from_zip(zip_bytes)

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
        """Get pipeline YAML/definition (v1 API).  Cached briefly."""
        cache_key = f"pipedef/{pipeline_id}/{branch or ''}"
        cached: dict | None = self._cache.get(cache_key)
        if cached is not None:
            return cached

        params: dict[str, str] = {"accountIdentifier": self._profile.account_id}
        if branch:
            params["branch"] = branch
        raw = await self._request(
            "GET",
            f"/v1/orgs/{self._profile.org_identifier}/projects/{self._profile.project_identifier}/pipelines/{pipeline_id}",
            params=params,
        )
        result = raw if isinstance(raw, dict) else {"pipeline_yaml": raw}
        self._cache.set(cache_key, result, ttl=_PIPELINE_DEF_CACHE_TTL)
        return result

    async def list_pipelines(
        self, search: str | None = None
    ) -> list[PipelineSummary]:
        """List all pipelines in current project (v1 API, paginated, cached)."""
        cache_key = f"pipelines/{search or ''}"
        cached: list[PipelineSummary] | None = self._cache.get(cache_key)
        if cached is not None:
            return cached

        params: dict[str, str] = {
            "accountIdentifier": self._profile.account_id,
            "limit": str(_PAGE_SIZE),
        }
        if search:
            params["search_term"] = search
        items = await self._fetch_pages(
            "GET",
            f"/v1/orgs/{self._profile.org_identifier}/projects/{self._profile.project_identifier}/pipelines",
            params=params,
        )
        result = parse_pipeline_list(items)
        self._cache.set(cache_key, result, ttl=_LIST_CACHE_TTL)
        return result

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
            "size": str(_PAGE_SIZE),
        }
        if search:
            params["searchTerm"] = search
        body: dict[str, Any] = {"filterType": "Template"}
        if template_type:
            body["templateEntityTypes"] = [template_type]
        items = await self._fetch_pages(
            "POST",
            "/template/api/templates/list",
            params=params,
            json_body=body,
            scope=scope,
        )
        return parse_template_list(items)

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
        params: dict[str, str] = {"pageSize": str(_PAGE_SIZE)}
        if search:
            params["searchTerm"] = search
        items = await self._fetch_pages(
            "GET", "/ng/api/v2/secrets", params=params, scope=scope,
            page_param="pageIndex",
        )
        return parse_secret_list(items)

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
        params: dict[str, str] = {"size": str(_PAGE_SIZE)}
        if search:
            params["searchTerm"] = search
        items = await self._fetch_pages("GET", "/ng/api/environmentsV2", params=params)
        return parse_environment_list(items)

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
        items = await self._fetch_pages(
            "GET",
            "/ng/api/infrastructures",
            params={"environmentIdentifier": environment_id, "size": str(_PAGE_SIZE)},
        )
        return parse_infrastructure_list(items)

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
        params: dict[str, str] = {"size": str(_PAGE_SIZE)}
        if search:
            params["searchTerm"] = search
        items = await self._fetch_pages("GET", "/ng/api/servicesV2", params=params)
        return parse_service_list(items)

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
        return parse_delegate_list(data)

    async def list_connectors(self, search: str | None = None) -> list[ConnectorInfo]:
        """List connectors with their last connectivity test status."""
        params: dict[str, str] = {"pageSize": str(_PAGE_SIZE)}
        if search:
            params["searchTerm"] = search
        items = await self._fetch_pages(
            "GET", "/ng/api/connectors", params=params, page_param="pageIndex"
        )
        return parse_connector_list(items)

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

        raw = await self._request(
            "POST",
            f"/ng/api/connectors/testConnection/{bare_id}",
            scope=scope,
        )
        data = self._unwrap_legacy(raw)
        return data if isinstance(data, dict) else {"status": "UNKNOWN"}
