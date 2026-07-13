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
    EnvironmentSummary,
    ExecutionGraph,
    ExecutionStatus,
    InfrastructureSummary,
    InputSet,
    PipelineExecution,
    PipelineSummary,
    SecretInfo,
    ServiceSummary,
    TemplateSummary,
)


class ProviderError(Exception):
    """Provider-neutral API error.

    Tools should catch this (not provider-specific subclasses) so they stay
    portable across providers.  ``status_code`` carries the HTTP status when
    the underlying provider is REST-based, else 0.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class PipelineProvider(ABC):
    """Abstract interface for CI/CD pipeline providers."""

    @abstractmethod
    async def get_execution(self, execution_id: str) -> PipelineExecution:
        """Get full execution details including all stages and steps."""

    @abstractmethod
    async def get_execution_graph(self, execution_id: str) -> ExecutionGraph:
        """Get the execution graph with node hierarchy."""

    @abstractmethod
    async def get_raw_execution_graph(self, execution_id: str) -> dict:  # type: ignore[type-arg]
        """Get the raw execution graph node map with provider-native fields.

        Used by investigation tools to access enriched node data
        (step parameters, executable responses) that the parsed
        ``ExecutionGraph`` model does not carry.  Providers without a raw
        graph concept may return ``{}``.
        """

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
    async def list_templates(
        self,
        search: str | None = None,
        template_type: str | None = None,
        scope: str = "project",
    ) -> list[TemplateSummary]:
        """List templates in the given scope (project, org, or account)."""

    @abstractmethod
    async def get_template(
        self,
        template_id: str,
        version_label: str | None = None,
        scope: str = "project",
    ) -> dict:  # type: ignore[type-arg]
        """Get template detail including YAML.

        When *version_label* is omitted, the stable version is returned.
        """

    @abstractmethod
    async def list_secrets(
        self, search: str | None = None, scope: str = "project"
    ) -> list[SecretInfo]:
        """List secret metadata in the given scope — never secret values."""

    @abstractmethod
    async def get_secret(self, secret_id: str, scope: str = "project") -> SecretInfo:
        """Get metadata for a single secret — never the secret value.

        A scope prefix in the identifier (``account.`` / ``org.``)
        overrides *scope*.
        """

    @abstractmethod
    async def list_environments(
        self, search: str | None = None
    ) -> list[EnvironmentSummary]:
        """List environments in the current project."""

    @abstractmethod
    async def get_environment(self, environment_id: str) -> dict:  # type: ignore[type-arg]
        """Get environment detail including YAML."""

    @abstractmethod
    async def list_infrastructures(
        self, environment_id: str
    ) -> list[InfrastructureSummary]:
        """List infrastructure definitions for an environment."""

    @abstractmethod
    async def get_infrastructure(
        self, infrastructure_id: str, environment_id: str
    ) -> dict:  # type: ignore[type-arg]
        """Get infrastructure definition detail including YAML."""

    @abstractmethod
    async def list_services(self, search: str | None = None) -> list[ServiceSummary]:
        """List services in the current project."""

    @abstractmethod
    async def get_service(self, service_id: str) -> dict:  # type: ignore[type-arg]
        """Get service detail including YAML."""

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
