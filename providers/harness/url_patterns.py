"""Harness URL parser — parse any Harness UI URL into structured components.

This is a killer feature: users paste URLs and Hawkeye extracts all the
identifiers needed to call the API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class ParsedHarnessUrl:
    """Structured representation of a Harness UI URL."""

    base_url: str
    account_id: str
    module: str | None = None
    org_identifier: str = ""
    project_identifier: str = ""
    pipeline_identifier: str = ""
    execution_id: str | None = None
    stage_id: str | None = None
    step_id: str | None = None
    child_stage_id: str | None = None
    stage_exec_id: str | None = None
    branch: str | None = None
    repo_name: str | None = None
    connector_ref: str | None = None
    store_type: str | None = None


# Regex that captures the path components of a Harness URL.
# Supports /ng/account/{acct}/module/{mod}/orgs/{org}/projects/{proj}/
# pipelines/{pipe}[/executions|deployments/{exec}/pipeline] and pipeline-studio URLs.
_PATH_RE = re.compile(
    r"/ng/account/(?P<account>[^/]+)"
    r"(?:/module/(?P<module>[^/]+)|/all)?"
    r"/orgs/(?P<org>[^/]+)"
    r"/projects/(?P<project>[^/]+)"
    r"/pipelines/(?P<pipeline>[^/]+)"
    r"(?:"
    r"/(?:executions|deployments)/(?P<execution>[^/?]+)(?:/pipeline)?"
    r"|/pipeline-studio"
    r")?"
)


def parse_harness_url(url: str) -> ParsedHarnessUrl:
    """Parse a Harness UI URL into its structured components.

    Raises ``ValueError`` for URLs that don't match the expected Harness format.
    """
    parsed = urlparse(url)
    match = _PATH_RE.search(parsed.path)
    if not match:
        raise ValueError(f"Not a recognised Harness URL: {url}")

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    qs = parse_qs(parsed.query)

    def _first(key: str) -> str | None:
        vals = qs.get(key)
        return vals[0] if vals else None

    return ParsedHarnessUrl(
        base_url=base_url,
        account_id=match.group("account"),
        module=match.group("module"),
        org_identifier=match.group("org"),
        project_identifier=match.group("project"),
        pipeline_identifier=match.group("pipeline"),
        execution_id=match.group("execution"),
        stage_id=_first("stage"),
        step_id=_first("step"),
        child_stage_id=_first("childStage"),
        stage_exec_id=_first("stageExecId"),
        branch=_first("branch"),
        repo_name=_first("repoName"),
        connector_ref=_first("connectorRef"),
        store_type=_first("storeType"),
    )
