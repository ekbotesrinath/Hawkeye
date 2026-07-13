"""Tests for the environments toolset — parser, client endpoints, and the 6 tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models import (
    EnvironmentSummary,
    HarnessProfile,
    InfrastructureSummary,
    ServiceSummary,
)
from providers.harness.client import HarnessClient
from providers.harness.parser import (
    parse_environment_list,
    parse_infrastructure_list,
    parse_service_list,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_environment(
    identifier: str = "prod",
    name: str = "Production",
    env_type: str = "Production",
) -> EnvironmentSummary:
    return EnvironmentSummary(
        identifier=identifier,
        name=name,
        env_type=env_type,
        scope="project",
        description="Production environment",
        tags={"tier": "1"},
        store_type="INLINE",
        created=1_690_000_000_000,
        last_modified=1_750_000_000_000,
    )


def _make_infrastructure(
    identifier: str = "prod_k8s",
    name: str = "Prod K8s Cluster",
) -> InfrastructureSummary:
    return InfrastructureSummary(
        identifier=identifier,
        name=name,
        environment_ref="prod",
        infra_type="KubernetesDirect",
        deployment_type="Kubernetes",
        description="Primary AKS cluster",
        created=1_690_000_000_000,
        last_modified=1_748_000_000_000,
    )


def _make_service(
    identifier: str = "payments_api",
    name: str = "Payments API",
) -> ServiceSummary:
    return ServiceSummary(
        identifier=identifier,
        name=name,
        description="Core payments microservice",
        scope="project",
        tags={"team": "payments"},
        store_type="INLINE",
        created=1_690_000_000_000,
        last_modified=1_749_000_000_000,
    )


_ENV_DETAIL = {
    "identifier": "prod",
    "name": "Production",
    "type": "Production",
    "description": "Production environment",
    "yaml": "environment:\n  name: Production\n  identifier: prod\n",
}

_INFRA_DETAIL = {
    "identifier": "prod_k8s",
    "name": "Prod K8s Cluster",
    "type": "KubernetesDirect",
    "deploymentType": "Kubernetes",
    "yaml": (
        "infrastructureDefinition:\n  identifier: prod_k8s\n  spec:\n"
        "    connectorRef: account.prod_cluster\n"
    ),
}

_SERVICE_DETAIL = {
    "identifier": "payments_api",
    "name": "Payments API",
    "description": "Core payments microservice",
    "yaml": "service:\n  name: Payments API\n  identifier: payments_api\n",
}


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.list_environments.return_value = []
    provider.list_infrastructures.return_value = []
    provider.list_services.return_value = []

    async def _get_provider():
        return provider

    with (
        patch("tools.environments.get_provider", side_effect=_get_provider),
        patch("tools._helpers.get_provider", side_effect=_get_provider),
        patch("tools._helpers.log_tool_call"),
    ):
        yield provider


@pytest.fixture()
def profile() -> HarnessProfile:
    return HarnessProfile(
        name="test-profile",
        account_id="acc123",
        org_identifier="default",
        project_identifier="hawkeye_proj",
        base_url="https://app.harness.io",
        is_active=True,
    )


@pytest.fixture()
def client(profile: HarnessProfile) -> HarnessClient:
    with patch("providers.harness.client.get_token", return_value="tok_secret"):
        yield HarnessClient(profile)


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------


class TestParseEnvironmentList:
    def test_parses_all_items(self, sample_environment_list_response: dict) -> None:
        items = sample_environment_list_response["data"]["content"]
        envs = parse_environment_list(items)
        assert len(envs) == 2
        assert envs[0].identifier == "prod"
        assert envs[0].env_type == "Production"
        assert envs[1].identifier == "staging"
        assert envs[1].env_type == "PreProduction"

    def test_timestamps(self, sample_environment_list_response: dict) -> None:
        items = sample_environment_list_response["data"]["content"]
        envs = parse_environment_list(items)
        assert envs[0].created == 1690000000000
        assert envs[0].last_modified == 1750000000000

    def test_non_dict_items_skipped(self) -> None:
        assert parse_environment_list(["bogus", None]) == []


class TestParseInfrastructureList:
    def test_parses_all_items(self, sample_infrastructure_list_response: dict) -> None:
        items = sample_infrastructure_list_response["data"]["content"]
        infras = parse_infrastructure_list(items)
        assert len(infras) == 2
        assert infras[0].identifier == "prod_k8s"
        assert infras[0].infra_type == "KubernetesDirect"
        assert infras[0].deployment_type == "Kubernetes"
        assert infras[0].environment_ref == "prod"
        assert infras[1].infra_type == "Pdc"


class TestParseServiceList:
    def test_parses_all_items(self, sample_service_list_response: dict) -> None:
        items = sample_service_list_response["data"]["content"]
        services = parse_service_list(items)
        assert len(services) == 2
        assert services[0].identifier == "payments_api"
        assert services[0].tags == {"team": "payments"}
        assert services[1].store_type == "REMOTE"


# ------------------------------------------------------------------
# Client endpoints
# ------------------------------------------------------------------


class TestEnvironmentEndpoints:
    @respx.mock
    @pytest.mark.asyncio
    async def test_list_environments(
        self, client: HarnessClient, sample_environment_list_response: dict
    ) -> None:
        route = respx.get("https://app.harness.io/ng/api/environmentsV2").mock(
            return_value=httpx.Response(200, json=sample_environment_list_response)
        )
        envs = await client.list_environments()
        assert route.called
        params = dict(httpx.QueryParams(route.calls[0].request.url.query))
        assert params["projectIdentifier"] == "hawkeye_proj"
        assert len(envs) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_environment_unwraps_entity(
        self, client: HarnessClient, sample_environment_list_response: dict
    ) -> None:
        single = {
            "status": "SUCCESS",
            "data": sample_environment_list_response["data"]["content"][0],
        }
        respx.get("https://app.harness.io/ng/api/environmentsV2/prod").mock(
            return_value=httpx.Response(200, json=single)
        )
        env = await client.get_environment("prod")
        assert env["identifier"] == "prod"
        assert "yaml" in env

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_infrastructures_passes_environment(
        self, client: HarnessClient, sample_infrastructure_list_response: dict
    ) -> None:
        route = respx.get("https://app.harness.io/ng/api/infrastructures").mock(
            return_value=httpx.Response(200, json=sample_infrastructure_list_response)
        )
        infras = await client.list_infrastructures("prod")
        params = dict(httpx.QueryParams(route.calls[0].request.url.query))
        assert params["environmentIdentifier"] == "prod"
        assert len(infras) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_service_unwraps_entity(
        self, client: HarnessClient, sample_service_list_response: dict
    ) -> None:
        single = {
            "status": "SUCCESS",
            "data": sample_service_list_response["data"]["content"][0],
        }
        respx.get("https://app.harness.io/ng/api/servicesV2/payments_api").mock(
            return_value=httpx.Response(200, json=single)
        )
        service = await client.get_service("payments_api")
        assert service["identifier"] == "payments_api"
        assert "yaml" in service


# ------------------------------------------------------------------
# Tool integration tests
# ------------------------------------------------------------------


class TestHawkeyeListEnvironments:
    @pytest.mark.asyncio
    async def test_output_format(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [
            _make_environment(),
            _make_environment(identifier="staging", name="Staging", env_type="PreProduction"),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_environments"].fn
        result = await tool_fn()
        assert "Environments (2 found)" in result
        assert "`prod`" in result
        assert "PreProduction" in result

    @pytest.mark.asyncio
    async def test_no_environments(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_environments"].fn
        result = await tool_fn(search="nothing")
        assert "No environments found" in result


class TestHawkeyeGetEnvironment:
    @pytest.mark.asyncio
    async def test_returns_yaml(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        mock_provider.get_environment.return_value = _ENV_DETAIL
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_environment"].fn
        result = await tool_fn(environment_id="prod")
        assert "Environment: Production (`prod`)" in result
        assert "```yaml" in result
        assert "**Type:** Production" in result

    @pytest.mark.asyncio
    async def test_fuzzy_resolution(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        mock_provider.get_environment.return_value = _ENV_DETAIL
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_environment"].fn
        await tool_fn(environment_id="Production")
        mock_provider.get_environment.assert_awaited_once_with("prod")

    @pytest.mark.asyncio
    async def test_not_found_suggestions(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_environment"].fn
        result = await tool_fn(environment_id="zzzzqqqq")
        assert "not found" in result
        mock_provider.get_environment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_id_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_environment"].fn
        result = await tool_fn(environment_id=" ")
        assert "Provide an environment identifier" in result


class TestHawkeyeListInfrastructures:
    @pytest.mark.asyncio
    async def test_output_format(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        mock_provider.list_infrastructures.return_value = [_make_infrastructure()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_infrastructures"].fn
        result = await tool_fn(environment_id="prod")
        assert "Infrastructure Definitions (1 found" in result
        assert "`prod_k8s`" in result
        assert "KubernetesDirect" in result

    @pytest.mark.asyncio
    async def test_env_fuzzy_resolution(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        mock_provider.list_infrastructures.return_value = [_make_infrastructure()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_infrastructures"].fn
        await tool_fn(environment_id="Production")
        mock_provider.list_infrastructures.assert_awaited_once_with("prod")

    @pytest.mark.asyncio
    async def test_no_infrastructures(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_infrastructures"].fn
        result = await tool_fn(environment_id="prod")
        assert "No infrastructure definitions found" in result


class TestHawkeyeGetInfrastructure:
    @pytest.mark.asyncio
    async def test_returns_yaml(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        mock_provider.list_infrastructures.return_value = [_make_infrastructure()]
        mock_provider.get_infrastructure.return_value = _INFRA_DETAIL
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_infrastructure"].fn
        result = await tool_fn(infrastructure_id="prod_k8s", environment_id="prod")
        assert "Infrastructure: Prod K8s Cluster (`prod_k8s`)" in result
        assert "connectorRef: account.prod_cluster" in result
        mock_provider.get_infrastructure.assert_awaited_once_with("prod_k8s", "prod")

    @pytest.mark.asyncio
    async def test_fuzzy_infra_resolution(self, mock_provider) -> None:
        mock_provider.list_environments.return_value = [_make_environment()]
        mock_provider.list_infrastructures.return_value = [_make_infrastructure()]
        mock_provider.get_infrastructure.return_value = _INFRA_DETAIL
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_infrastructure"].fn
        await tool_fn(infrastructure_id="k8s cluster", environment_id="prod")
        mock_provider.get_infrastructure.assert_awaited_once_with("prod_k8s", "prod")

    @pytest.mark.asyncio
    async def test_missing_environment_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_infrastructure"].fn
        result = await tool_fn(infrastructure_id="prod_k8s", environment_id="")
        assert "environment identifier" in result
        mock_provider.get_infrastructure.assert_not_awaited()


class TestHawkeyeListServices:
    @pytest.mark.asyncio
    async def test_output_format(self, mock_provider) -> None:
        mock_provider.list_services.return_value = [_make_service()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_services"].fn
        result = await tool_fn()
        assert "Services (1 found)" in result
        assert "`payments_api`" in result

    @pytest.mark.asyncio
    async def test_search_passed_to_provider(self, mock_provider) -> None:
        mock_provider.list_services.return_value = [_make_service()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_services"].fn
        await tool_fn(search="payments")
        mock_provider.list_services.assert_awaited_once_with(search="payments")

    @pytest.mark.asyncio
    async def test_no_services(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_services"].fn
        result = await tool_fn(search="nothing")
        assert "No services found" in result


class TestHawkeyeGetService:
    @pytest.mark.asyncio
    async def test_returns_yaml(self, mock_provider) -> None:
        mock_provider.list_services.return_value = [_make_service()]
        mock_provider.get_service.return_value = _SERVICE_DETAIL
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_service"].fn
        result = await tool_fn(service_id="payments_api")
        assert "Service: Payments API (`payments_api`)" in result
        assert "```yaml" in result
        mock_provider.get_service.assert_awaited_once_with("payments_api")

    @pytest.mark.asyncio
    async def test_fuzzy_resolution_by_name(self, mock_provider) -> None:
        mock_provider.list_services.return_value = [_make_service()]
        mock_provider.get_service.return_value = _SERVICE_DETAIL
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_service"].fn
        await tool_fn(service_id="Payments API")
        mock_provider.get_service.assert_awaited_once_with("payments_api")

    @pytest.mark.asyncio
    async def test_empty_id_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_service"].fn
        result = await tool_fn(service_id="")
        assert "Provide a service identifier" in result
        mock_provider.get_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_yaml_missing_falls_back_to_json(self, mock_provider) -> None:
        mock_provider.list_services.return_value = [_make_service()]
        mock_provider.get_service.return_value = {
            "identifier": "payments_api",
            "name": "Payments API",
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_service"].fn
        result = await tool_fn(service_id="payments_api")
        assert '"identifier"' in result
