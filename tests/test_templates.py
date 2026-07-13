"""Tests for tools/templates.py — the 2 template tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.models import TemplateSummary
from tools.templates import (
    _format_template_detail,
    _format_template_table,
    _split_scope_prefix,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_template_summary(
    identifier: str = "deploy_step_template",
    name: str = "Deploy Step Template",
    version_label: str = "v2",
    entity_type: str = "Step",
    child_type: str | None = "K8sRollingDeploy",
    scope: str = "project",
    stable: bool = True,
) -> TemplateSummary:
    return TemplateSummary(
        identifier=identifier,
        name=name,
        version_label=version_label,
        template_entity_type=entity_type,
        child_type=child_type,
        scope=scope,
        stable_template=stable,
        description="A test template",
        tags={"team": "platform"},
        store_type="INLINE",
        last_updated=1_700_000_000_000,
    )


def _make_template_detail(
    identifier: str = "deploy_step_template",
    name: str = "Deploy Step Template",
    yaml_content: str = "template:\n  name: Deploy Step Template",
) -> dict:
    return {
        "identifier": identifier,
        "name": name,
        "yaml": yaml_content,
        "versionLabel": "v2",
        "templateEntityType": "Step",
        "childType": "K8sRollingDeploy",
        "templateScope": "project",
        "stableTemplate": True,
        "description": "A test template",
    }


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.list_templates.return_value = []

    async def _get_provider():
        return provider

    with (
        patch("tools.templates.get_provider", side_effect=_get_provider),
        patch("tools._helpers.get_provider", side_effect=_get_provider),
        patch("tools._helpers.log_tool_call"),
    ):
        yield provider


# ------------------------------------------------------------------
# Scope prefix parsing
# ------------------------------------------------------------------


class TestSplitScopePrefix:
    def test_account_prefix(self) -> None:
        assert _split_scope_prefix("account.tmpl") == ("account", "tmpl")

    def test_org_prefix(self) -> None:
        assert _split_scope_prefix("org.tmpl") == ("org", "tmpl")

    def test_no_prefix_defaults_to_project(self) -> None:
        assert _split_scope_prefix("tmpl") == ("project", "tmpl")


# ------------------------------------------------------------------
# Template table formatting
# ------------------------------------------------------------------


class TestFormatTemplateTable:
    def test_basic_table(self) -> None:
        templates = [_make_template_summary()]
        result = _format_template_table(templates, "project")
        assert "Templates (1 found, project scope)" in result
        assert "Deploy Step Template" in result
        assert "deploy_step_template" in result
        assert "K8sRollingDeploy" in result
        assert "v2" in result

    def test_multiple_templates(self) -> None:
        templates = [
            _make_template_summary(identifier="t1", name="Template A"),
            _make_template_summary(identifier="t2", name="Template B", entity_type="Stage"),
        ]
        result = _format_template_table(templates, "project")
        assert "2 found" in result
        assert "Template A" in result
        assert "Template B" in result

    def test_missing_fields_show_dash(self) -> None:
        templates = [
            _make_template_summary(
                child_type=None, version_label="", entity_type="", stable=False
            )
        ]
        result = _format_template_table(templates, "project")
        assert "—" in result

    def test_stable_flag_icon(self) -> None:
        templates = [_make_template_summary(stable=True)]
        result = _format_template_table(templates, "project")
        assert "✅" in result


# ------------------------------------------------------------------
# Template detail formatting
# ------------------------------------------------------------------


class TestFormatTemplateDetail:
    def test_basic_detail(self) -> None:
        detail = _make_template_detail()
        result = _format_template_detail("deploy_step_template", detail, "project")
        assert "Template: Deploy Step Template" in result
        assert "`deploy_step_template`" in result
        assert "```yaml" in result
        assert "template:" in result

    def test_metadata_line(self) -> None:
        detail = _make_template_detail()
        result = _format_template_detail("deploy_step_template", detail, "project")
        assert "**Type:** Step / K8sRollingDeploy" in result
        assert "**Version:** v2 (stable)" in result
        assert "**Scope:** project" in result

    def test_description_included(self) -> None:
        detail = _make_template_detail()
        result = _format_template_detail("deploy_step_template", detail, "project")
        assert "A test template" in result

    def test_missing_yaml_falls_back_to_json(self) -> None:
        detail = {"identifier": "t1", "name": "T1", "versionLabel": "v1"}
        result = _format_template_detail("t1", detail, "project")
        assert '"identifier"' in result

    def test_non_stable_version_has_no_stable_marker(self) -> None:
        detail = _make_template_detail()
        detail["stableTemplate"] = False
        result = _format_template_detail("deploy_step_template", detail, "project")
        assert "(stable)" not in result


# ------------------------------------------------------------------
# Tool integration tests
# ------------------------------------------------------------------


class TestHawkeyeListTemplates:
    @pytest.mark.asyncio
    async def test_output_format(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [_make_template_summary()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        result = await tool_fn()
        assert "Deploy Step Template" in result

    @pytest.mark.asyncio
    async def test_search_passed_to_provider(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [_make_template_summary()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        await tool_fn(search="deploy")
        mock_provider.list_templates.assert_awaited_once_with(
            search="deploy", template_type=None, scope="project"
        )

    @pytest.mark.asyncio
    async def test_template_type_normalized(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [_make_template_summary()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        await tool_fn(template_type="step")
        mock_provider.list_templates.assert_awaited_once_with(
            search=None, template_type="Step", scope="project"
        )

    @pytest.mark.asyncio
    async def test_invalid_template_type_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        result = await tool_fn(template_type="bogus")
        assert "Unknown template type" in result
        mock_provider.list_templates.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_scope_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        result = await tool_fn(scope="galaxy")
        assert "Unknown scope" in result
        mock_provider.list_templates.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_scope_passed_through(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        result = await tool_fn(scope="account")
        mock_provider.list_templates.assert_awaited_once_with(
            search=None, template_type=None, scope="account"
        )
        assert "account scope" in result

    @pytest.mark.asyncio
    async def test_no_templates_found(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_templates"].fn
        result = await tool_fn(search="nothing")
        assert "No templates found" in result
        assert "nothing" in result


class TestHawkeyeGetTemplate:
    @pytest.mark.asyncio
    async def test_returns_yaml(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [_make_template_summary()]
        mock_provider.get_template.return_value = _make_template_detail()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        result = await tool_fn(template_id="deploy_step_template")
        assert "```yaml" in result
        assert "template:" in result
        mock_provider.get_template.assert_awaited_once_with(
            "deploy_step_template", version_label=None, scope="project"
        )

    @pytest.mark.asyncio
    async def test_version_label_passed_through(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [_make_template_summary()]
        mock_provider.get_template.return_value = _make_template_detail()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        await tool_fn(template_id="deploy_step_template", version_label="v1")
        mock_provider.get_template.assert_awaited_once_with(
            "deploy_step_template", version_label="v1", scope="project"
        )

    @pytest.mark.asyncio
    async def test_fuzzy_resolution_of_partial_id(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [_make_template_summary()]
        mock_provider.get_template.return_value = _make_template_detail()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        await tool_fn(template_id="deploy_step")
        mock_provider.get_template.assert_awaited_once_with(
            "deploy_step_template", version_label=None, scope="project"
        )

    @pytest.mark.asyncio
    async def test_fuzzy_resolution_by_display_name(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [
            _make_template_summary(identifier="tmpl_xyz", name="Golden Deploy")
        ]
        mock_provider.get_template.return_value = _make_template_detail(
            identifier="tmpl_xyz", name="Golden Deploy"
        )
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        await tool_fn(template_id="Golden Deploy")
        mock_provider.get_template.assert_awaited_once_with(
            "tmpl_xyz", version_label=None, scope="project"
        )

    @pytest.mark.asyncio
    async def test_not_found_returns_suggestions(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = [
            _make_template_summary(identifier="alpha_template", name="Alpha"),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        result = await tool_fn(template_id="zzzzqqqq")
        assert "not found" in result
        mock_provider.get_template.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_prefix_overrides_scope(self, mock_provider) -> None:
        mock_provider.list_templates.return_value = []
        mock_provider.get_template.return_value = _make_template_detail(
            identifier="shared_tmpl", name="Shared"
        )
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        await tool_fn(template_id="account.shared_tmpl")
        # Listing (for fuzzy resolution) and fetch both run in account scope
        mock_provider.list_templates.assert_awaited_once_with(scope="account")
        mock_provider.get_template.assert_awaited_once_with(
            "shared_tmpl", version_label=None, scope="account"
        )

    @pytest.mark.asyncio
    async def test_empty_template_id_returns_error(self, mock_provider) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        result = await tool_fn(template_id="  ")
        assert "Provide a template identifier" in result
        mock_provider.get_template.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_id_skips_fuzzy_when_no_templates_listed(self, mock_provider) -> None:
        """When listing yields nothing, the ID passes through unchanged."""
        mock_provider.list_templates.return_value = []
        mock_provider.get_template.return_value = _make_template_detail()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_template"].fn
        await tool_fn(template_id="deploy_step_template")
        mock_provider.get_template.assert_awaited_once_with(
            "deploy_step_template", version_label=None, scope="project"
        )
