"""Pydantic data models for Hawkeye pipeline observability."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ExecutionStatus(StrEnum):
    """All known pipeline execution statuses from Harness."""

    SUCCESS = "Success"
    FAILED = "Failed"
    ABORTED = "Aborted"
    EXPIRED = "Expired"
    RUNNING = "Running"
    ASYNC_WAITING = "AsyncWaiting"
    TASK_WAITING = "TaskWaiting"
    TIMED_WAITING = "TimedWaiting"
    ERRORED = "Errored"
    IGNORE_FAILED = "IgnoreFailed"
    NOT_STARTED = "NotStarted"
    QUEUED = "Queued"
    PAUSED = "Paused"
    ABORTED_BY_FREEZE = "AbortedByFreeze"
    APPROVAL_WAITING = "ApprovalWaiting"
    INTERVENTION_WAITING = "InterventionWaiting"
    APPROVAL_REJECTED = "ApprovalRejected"
    INPUT_WAITING = "InputWaiting"
    WAIT_STEP_RUNNING = "WaitStepRunning"
    SUSPENDED = "Suspended"
    SKIPPED = "Skipped"
    UNKNOWN = "Unknown"

    @classmethod
    def from_str(cls, value: str) -> ExecutionStatus:
        """Parse a status string, returning UNKNOWN for unrecognised values."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


# Canonical status groupings shared across tools — define once here, not
# per-module.
FAILED_STATUSES: frozenset[ExecutionStatus] = frozenset({
    ExecutionStatus.FAILED,
    ExecutionStatus.ERRORED,
    ExecutionStatus.EXPIRED,
})

SKIPPED_STATUSES: frozenset[ExecutionStatus] = frozenset({
    ExecutionStatus.SKIPPED,
    ExecutionStatus.NOT_STARTED,
})

# Statuses after which an execution can no longer change
TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset({
    ExecutionStatus.SUCCESS,
    ExecutionStatus.FAILED,
    ExecutionStatus.ERRORED,
    ExecutionStatus.ABORTED,
    ExecutionStatus.EXPIRED,
    ExecutionStatus.APPROVAL_REJECTED,
    ExecutionStatus.ABORTED_BY_FREEZE,
    ExecutionStatus.IGNORE_FAILED,
})

# Statuses that indicate a human needs to act
WAITING_ON_HUMAN_STATUSES: frozenset[ExecutionStatus] = frozenset({
    ExecutionStatus.APPROVAL_WAITING,
    ExecutionStatus.INTERVENTION_WAITING,
    ExecutionStatus.INPUT_WAITING,
})


class FailureInfo(BaseModel):
    """Failure details for a stage or step."""

    model_config = {"frozen": True}

    message: str = Field(default="", description="Human-readable failure message")
    failure_types: list[str] = Field(
        default_factory=list,
        description="Failure type codes (APPLICATION_FAILURE, TIMEOUT_ERROR, …)",
    )
    response_messages: list[str] = Field(
        default_factory=list,
        description="Additional response messages from the platform",
    )


class StepExecution(BaseModel):
    """Single step within a stage."""

    model_config = {"frozen": True}

    step_id: str = Field(description="Unique step execution identifier")
    name: str = Field(description="Display name of the step")
    identifier: str = Field(description="Pipeline-level step identifier")
    status: ExecutionStatus = Field(description="Current execution status")
    step_type: str = Field(
        default="Unknown",
        description="Step type (ShellScript, Run, K8sRollingDeploy, …)",
    )
    start_ts: int | None = Field(default=None, description="Start time in Unix ms")
    end_ts: int | None = Field(default=None, description="End time in Unix ms")
    failure_info: FailureInfo | None = Field(default=None, description="Failure details if failed")
    delegate_info: str | None = Field(
        default=None, description="Delegate that executed this step"
    )
    outcomes: dict | None = Field(default=None, description="Step outcome metadata")  # type: ignore[type-arg]
    group_path: str = Field(
        default="",
        description=(
            "Step-group path within the stage (e.g. 'deploy_apim', or "
            "'rollbackSteps' for rollback twins); empty for top-level steps"
        ),
    )


class StageExecution(BaseModel):
    """Single stage within a pipeline execution."""

    model_config = {"frozen": True}

    stage_id: str = Field(description="Unique stage execution identifier")
    name: str = Field(description="Display name of the stage")
    identifier: str = Field(description="Pipeline-level stage identifier")
    status: ExecutionStatus = Field(description="Current execution status")
    start_ts: int | None = Field(default=None, description="Start time in Unix ms")
    end_ts: int | None = Field(default=None, description="End time in Unix ms")
    stage_type: str = Field(
        default="Unknown",
        description="Stage type (CI, CD, Approval, Custom, …)",
    )
    failure_info: FailureInfo | None = Field(default=None, description="Failure details if failed")
    steps: list[StepExecution] = Field(
        default_factory=list, description="Steps within this stage"
    )
    child_execution_id: str | None = Field(
        default=None,
        description="Child pipeline plan execution ID for Pipeline-type (chained) stages",
    )


class PipelineExecution(BaseModel):
    """Complete pipeline execution record."""

    model_config = {"frozen": True}

    plan_execution_id: str = Field(description="Harness plan execution ID")
    pipeline_identifier: str = Field(description="Pipeline identifier")
    pipeline_name: str = Field(description="Display name of the pipeline")
    status: ExecutionStatus = Field(description="Overall execution status")
    start_ts: int | None = Field(default=None, description="Start time in Unix ms")
    end_ts: int | None = Field(default=None, description="End time in Unix ms")
    trigger_type: str = Field(
        default="MANUAL",
        description="Trigger type (MANUAL, WEBHOOK, SCHEDULER, WEBHOOK_CUSTOM)",
    )
    triggered_by: str = Field(default="", description="User or entity that triggered the run")
    branch: str | None = Field(default=None, description="Source branch if applicable")
    run_sequence: int = Field(default=0, description="Monotonic run sequence number")
    execution_url: str = Field(default="", description="Deep link back to Harness UI")
    stages: list[StageExecution] = Field(
        default_factory=list, description="Stages in this execution"
    )
    execution_input_yaml: str | None = Field(
        default=None, description="Input YAML used for this execution"
    )
    tags: dict[str, str] = Field(default_factory=dict, description="User-defined tags")
    service_identifiers: list[str] = Field(
        default_factory=list,
        description="CD services deployed by this execution (from moduleInfo)",
    )
    env_identifiers: list[str] = Field(
        default_factory=list,
        description="CD environments targeted by this execution (from moduleInfo)",
    )


class PipelineSummary(BaseModel):
    """Lightweight pipeline listing entry."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Pipeline identifier")
    name: str = Field(description="Display name")
    description: str | None = Field(default=None, description="Pipeline description")
    modules: list[str] = Field(default_factory=list, description="Modules (CI, CD)")
    last_execution_status: ExecutionStatus | None = Field(
        default=None, description="Status of the most recent execution"
    )
    last_execution_ts: int | None = Field(
        default=None, description="Timestamp of most recent execution in Unix ms"
    )
    store_type: str = Field(default="INLINE", description="INLINE or REMOTE")
    repo_name: str | None = Field(default=None, description="Git repo name if remote")
    created: int = Field(default=0, description="Creation timestamp in Unix ms")
    updated: int = Field(default=0, description="Last update timestamp in Unix ms")


class InputSet(BaseModel):
    """Pipeline input set definition."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Input set identifier")
    name: str = Field(description="Display name")
    pipeline_identifier: str = Field(description="Parent pipeline identifier")
    input_set_yaml: str = Field(default="", description="YAML content of the input set")
    created: int | None = Field(default=None, description="Creation timestamp in Unix ms")
    updated: int | None = Field(default=None, description="Last update timestamp in Unix ms")


class TemplateSummary(BaseModel):
    """Lightweight template listing entry."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Template identifier")
    name: str = Field(description="Display name")
    version_label: str = Field(default="", description="Version label of this template version")
    template_entity_type: str = Field(
        default="",
        description="Template type (Step, Stage, Pipeline, StepGroup, …)",
    )
    child_type: str | None = Field(
        default=None, description="Child entity type (ShellScript, Deployment, CI, …)"
    )
    scope: str = Field(default="project", description="Scope: account, org, or project")
    stable_template: bool = Field(
        default=False, description="Whether this version is the stable one"
    )
    description: str | None = Field(default=None, description="Template description")
    tags: dict[str, str] = Field(default_factory=dict, description="User-defined tags")
    store_type: str = Field(default="INLINE", description="INLINE or REMOTE")
    last_updated: int | None = Field(
        default=None, description="Last update timestamp in Unix ms"
    )


class GraphNode(BaseModel):
    """Single node in the execution graph."""

    model_config = {"frozen": True}

    uuid: str = Field(description="Unique node identifier in the graph")
    name: str = Field(description="Display name of the node")
    identifier: str = Field(description="Pipeline-level identifier")
    step_type: str = Field(default="Unknown", description="Node type")
    status: ExecutionStatus = Field(description="Current execution status")
    start_ts: int | None = Field(default=None, description="Start time in Unix ms")
    end_ts: int | None = Field(default=None, description="End time in Unix ms")
    failure_info: FailureInfo | None = Field(default=None, description="Failure details if failed")
    outcomes: dict | None = Field(default=None, description="Node outcome metadata")  # type: ignore[type-arg]
    base_fqn: str | None = Field(
        default=None, description="Fully qualified name in pipeline hierarchy"
    )


class ExecutionGraph(BaseModel):
    """Parsed execution graph with node hierarchy."""

    model_config = {"frozen": True}

    root_node_id: str = Field(description="Root node identifier")
    nodes: dict[str, GraphNode] = Field(
        default_factory=dict, description="All nodes keyed by UUID"
    )


class DelegateInfo(BaseModel):
    """Delegate group information from Harness."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Delegate group identifier")
    name: str = Field(description="Display name")
    status: str = Field(default="UNKNOWN", description="Connection status")
    actively_connected: bool = Field(
        default=False, description="Whether delegate is actively connected"
    )
    last_heartbeat: int | None = Field(
        default=None, description="Last heartbeat timestamp in Unix ms"
    )
    delegate_type: str = Field(default="", description="Delegate type (KUBERNETES, DOCKER, SHELL)")
    tags: list[str] = Field(default_factory=list, description="Implicit selector tags")
    instances_connected: int = Field(
        default=0, description="Number of connected delegate instances"
    )


class ConnectorInfo(BaseModel):
    """Connector information from Harness."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Connector identifier")
    name: str = Field(description="Display name")
    connector_type: str = Field(
        default="", description="Connector type (Git, DockerRegistry, Aws, Azure, etc.)"
    )
    status: str = Field(default="UNKNOWN", description="Last connectivity test status")
    last_tested: int | None = Field(
        default=None, description="Last connectivity test timestamp in Unix ms"
    )
    scope: str = Field(default="project", description="Scope: account, org, or project")
    delegate_used: str = Field(default="", description="Delegate used for last connectivity test")
    error_summary: str = Field(default="", description="Error summary from last test")


class SecretInfo(BaseModel):
    """Secret metadata from Harness — never contains the secret value."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Secret identifier")
    name: str = Field(description="Display name")
    secret_type: str = Field(
        default="",
        description="Secret type (SecretText, SecretFile, SSHKey, WinRmCredentials)",
    )
    secret_manager: str = Field(
        default="", description="Identifier of the Secret Manager storing this secret"
    )
    value_type: str = Field(
        default="", description="Inline, Reference, or CustomSecretManagerValues"
    )
    scope: str = Field(default="project", description="Scope: account, org, or project")
    description: str | None = Field(default=None, description="Secret description")
    tags: dict[str, str] = Field(default_factory=dict, description="User-defined tags")
    created: int | None = Field(default=None, description="Creation timestamp in Unix ms")
    last_modified: int | None = Field(
        default=None, description="Last modification timestamp in Unix ms"
    )
    draft: bool = Field(default=False, description="Whether the secret is a draft")


class EnvironmentSummary(BaseModel):
    """Environment listing entry from Harness."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Environment identifier")
    name: str = Field(description="Display name")
    env_type: str = Field(
        default="", description="Environment type (Production, PreProduction)"
    )
    scope: str = Field(default="project", description="Scope: account, org, or project")
    description: str | None = Field(default=None, description="Environment description")
    tags: dict[str, str] = Field(default_factory=dict, description="User-defined tags")
    store_type: str = Field(default="INLINE", description="INLINE or REMOTE")
    created: int | None = Field(default=None, description="Creation timestamp in Unix ms")
    last_modified: int | None = Field(
        default=None, description="Last modification timestamp in Unix ms"
    )


class InfrastructureSummary(BaseModel):
    """Infrastructure definition listing entry from Harness."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Infrastructure definition identifier")
    name: str = Field(description="Display name")
    environment_ref: str = Field(default="", description="Parent environment identifier")
    infra_type: str = Field(
        default="",
        description="Infrastructure type (KubernetesDirect, KubernetesAzure, Pdc, …)",
    )
    deployment_type: str = Field(
        default="", description="Deployment type (Kubernetes, NativeHelm, Ssh, …)"
    )
    description: str | None = Field(default=None, description="Infrastructure description")
    created: int | None = Field(default=None, description="Creation timestamp in Unix ms")
    last_modified: int | None = Field(
        default=None, description="Last modification timestamp in Unix ms"
    )


class ServiceSummary(BaseModel):
    """Service listing entry from Harness."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Service identifier")
    name: str = Field(description="Display name")
    description: str | None = Field(default=None, description="Service description")
    deployment_type: str = Field(
        default="", description="Deployment type when reported by the API"
    )
    scope: str = Field(default="project", description="Scope: account, org, or project")
    tags: dict[str, str] = Field(default_factory=dict, description="User-defined tags")
    store_type: str = Field(default="INLINE", description="INLINE or REMOTE")
    created: int | None = Field(default=None, description="Creation timestamp in Unix ms")
    last_modified: int | None = Field(
        default=None, description="Last modification timestamp in Unix ms"
    )


class HarnessProfile(BaseModel):
    """Multi-tenant connection profile."""

    name: str = Field(description="Profile name (unique identifier)")
    account_id: str = Field(description="Harness account identifier")
    org_identifier: str = Field(description="Default organization identifier")
    project_identifier: str = Field(description="Default project identifier")
    base_url: str = Field(
        default="https://app.harness.io", description="Harness base URL"
    )
    is_active: bool = Field(default=False, description="Whether this is the active profile")
