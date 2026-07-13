"""Execution-graph search and console-log extraction for Harness.

Domain logic that used to live inline in the HTTP client: finding the graph
node that matches a user-supplied stage/step reference, extracting Log
Service keys, and converting downloaded NDJSON zip archives into readable
console text.  The client stays thin HTTP + retry; this module owns the
matching rules.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import Any

from core.models import ExecutionStatus

logger = logging.getLogger(__name__)

_MAX_LOG_DECOMPRESSED_BYTES = 20 * 1024 * 1024  # 20 MB extracted


# ------------------------------------------------------------------
# Stage/step matching
# ------------------------------------------------------------------


def _normalize_lookup_token(value: str) -> str:
    """Normalize a human or pipeline identifier for fuzzy equality checks."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _tokenize_lookup_value(value: str) -> set[str]:
    """Split an identifier or display name into normalized word tokens."""
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if token}


def extract_stage_identifier(base_fqn: str) -> str | None:
    """Extract the stage identifier segment from a baseFqn string."""
    parts = base_fqn.split(".") if base_fqn else []
    try:
        index = parts.index("stages")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    return parts[index + 1]


def stage_matches(
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

    extracted = extract_stage_identifier(base_fqn)
    if not extracted:
        return False

    if _normalize_lookup_token(extracted) == _normalize_lookup_token(stage_id):
        return True

    stage_tokens = _tokenize_lookup_value(stage_id)
    extracted_tokens = _tokenize_lookup_value(extracted)
    return bool(stage_tokens) and stage_tokens.issubset(extracted_tokens)


def candidate_score(node: Any, *, step_id: str, stage_matched: bool, uuid: str) -> int:
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


# ------------------------------------------------------------------
# Log key extraction
# ------------------------------------------------------------------


def extract_log_key(
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


# ------------------------------------------------------------------
# Zip / NDJSON log extraction
# ------------------------------------------------------------------


def extract_logs_from_zip(zip_bytes: bytes) -> str | None:
    """Extract readable log lines from a Harness logs.zip payload.

    Total decompressed output is capped to protect against oversized or
    malicious archives; a truncation note is appended when the cap is hit.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = sorted(name for name in archive.namelist() if not name.endswith("/"))
            sections: list[str] = []
            extracted = 0
            truncated = False
            for name in names:
                info = archive.getinfo(name)
                if extracted + info.file_size > _MAX_LOG_DECOMPRESSED_BYTES:
                    truncated = True
                    break
                raw = archive.read(name)
                extracted += len(raw)
                text = raw.decode("utf-8", errors="replace")
                lines = _extract_lines_from_ndjson(text)
                if not lines:
                    continue
                if len(names) > 1:
                    sections.append(f"== {name} ==")
                sections.extend(lines)
            if truncated:
                sections.append(
                    f"… (log archive truncated at {_MAX_LOG_DECOMPRESSED_BYTES} bytes)"
                )
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
