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
    actively_connected: bool = Field(default=False, description="Whether delegate is actively connected")
    last_heartbeat: int | None = Field(default=None, description="Last heartbeat timestamp in Unix ms")
    delegate_type: str = Field(default="", description="Delegate type (KUBERNETES, DOCKER, SHELL)")
    tags: list[str] = Field(default_factory=list, description="Implicit selector tags")
    instances_connected: int = Field(default=0, description="Number of connected delegate instances")


class ConnectorInfo(BaseModel):
    """Connector information from Harness."""

    model_config = {"frozen": True}

    identifier: str = Field(description="Connector identifier")
    name: str = Field(description="Display name")
    connector_type: str = Field(default="", description="Connector type (Git, DockerRegistry, Aws, Azure, etc.)")
    status: str = Field(default="UNKNOWN", description="Last connectivity test status")
    last_tested: int | None = Field(default=None, description="Last connectivity test timestamp in Unix ms")
    scope: str = Field(default="project", description="Scope: account, org, or project")
    delegate_used: str = Field(default="", description="Delegate used for last connectivity test")
    error_summary: str = Field(default="", description="Error summary from last test")


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
