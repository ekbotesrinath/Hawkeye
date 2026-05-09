"""Abstract provider interface for CI/CD pipeline providers.

Every provider (Harness, Jenkins, GitHub Actions, Azure DevOps) must
implement this interface.  Tools in ``tools/*.py`` call these methods —
they never call provider-specific code directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import (
    ConnectorInfo,
    DelegateInfo,
    ExecutionGraph,
    ExecutionStatus,
    InputSet,
    PipelineExecution,
    PipelineSummary,
)


class PipelineProvider(ABC):
    """Abstract interface for CI/CD pipeline providers."""

    @abstractmethod
    async def get_execution(self, execution_id: str) -> PipelineExecution:
        """Get full execution details including all stages and steps."""

    @abstractmethod
    async def get_execution_graph(self, execution_id: str) -> ExecutionGraph:
        """Get the execution graph with node hierarchy."""

    @abstractmethod
    async def get_step_logs(
        self, execution_id: str, stage_id: str, step_id: str
    ) -> dict:
        """Get step execution details from execution graph and subgraph.

        Returns a dict with 'node' (graph node data) and 'subgraph'
        (enriched subgraph data) keys.
        """

    @abstractmethod
    async def get_step_subgraph(
        self, execution_id: str, node_execution_id: str
    ) -> dict:
        """Get detailed subgraph for a specific node execution."""

    @abstractmethod
    async def list_recent_executions(
        self,
        pipeline_id: str,
        limit: int = 10,
        status: ExecutionStatus | None = None,
    ) -> list[PipelineExecution]:
        """List recent executions for a pipeline."""

    @abstractmethod
    async def get_pipeline_definition(self, pipeline_id: str, branch: str | None = None) -> dict:  # type: ignore[type-arg]
        """Get pipeline YAML/definition."""

    @abstractmethod
    async def list_pipelines(
        self, search: str | None = None
    ) -> list[PipelineSummary]:
        """List all pipelines in current project."""

    @abstractmethod
    async def get_input_sets(self, pipeline_id: str) -> list[InputSet]:
        """Get all input sets for a pipeline."""

    @abstractmethod
    async def get_execution_inputs(self, execution_id: str) -> str:
        """Get the input YAML used for a specific execution."""

    @abstractmethod
    async def test_connection(self) -> bool:
        """Verify API credentials and connectivity."""

    @abstractmethod
    async def list_delegates(self) -> list[DelegateInfo]:
        """List all delegate groups with health status."""

    @abstractmethod
    async def list_connectors(self, search: str | None = None) -> list[ConnectorInfo]:
        """List connectors with their last connectivity test status."""

    @abstractmethod
    async def test_connector(self, identifier: str, scope: str = "project") -> dict:
        """Test connectivity of a specific connector. Returns test result dict."""
