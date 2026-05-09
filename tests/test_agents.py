"""Tests for swarm agent files and copilot instructions."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / ".github" / "agents"
COPILOT_INSTRUCTIONS = PROJECT_ROOT / ".github" / "copilot-instructions.md"
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"
README_MD = PROJECT_ROOT / "README.md"

# All agent files expected
AGENT_FILES = [
    "hawkeye-lead.agent.md",
    "hawkeye-execution.agent.md",
    "hawkeye-logs.agent.md",
    "hawkeye-patterns.agent.md",
    "hawkeye-artifacts.agent.md",
]

# Every Hawkeye tool name registered in main.py
REGISTERED_TOOLS = [
    "hawkeye_get_execution",
    "hawkeye_get_stage_detail",
    "hawkeye_get_step_logs",
    "hawkeye_get_execution_inputs",
    "hawkeye_list_recent_executions",
    "hawkeye_get_pipeline_definition",
    "hawkeye_list_pipelines",
    "hawkeye_get_input_sets",
    "hawkeye_investigate_failure",
    "hawkeye_compare_executions",
    "hawkeye_get_failure_pattern",
    "hawkeye_connect_account",
    "hawkeye_list_profiles",
    "ping",
]


class TestAgentFilesExist:
    """Verify each agent file exists and is valid markdown."""

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_agent_file_exists(self, filename: str) -> None:
        path = AGENTS_DIR / filename
        assert path.exists(), f"Agent file missing: {path}"

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_agent_file_is_nonempty(self, filename: str) -> None:
        path = AGENTS_DIR / filename
        content = path.read_text(encoding="utf-8")
        assert len(content.strip()) > 100, f"Agent file too short: {filename}"

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_agent_file_has_frontmatter(self, filename: str) -> None:
        path = AGENTS_DIR / filename
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---"), f"Agent file missing YAML frontmatter: {filename}"
        # Must have closing frontmatter
        second_fence = content.index("---", 3)
        assert second_fence > 3, f"Agent file has malformed frontmatter: {filename}"

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_agent_file_has_name(self, filename: str) -> None:
        path = AGENTS_DIR / filename
        content = path.read_text(encoding="utf-8")
        assert "name:" in content, f"Agent file missing 'name:' in frontmatter: {filename}"

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_agent_file_references_hawkeye_tools(self, filename: str) -> None:
        path = AGENTS_DIR / filename
        content = path.read_text(encoding="utf-8")
        # Every agent must reference at least one Hawkeye tool
        has_tool_ref = any(tool in content for tool in REGISTERED_TOOLS)
        assert has_tool_ref, f"Agent file references no registered tools: {filename}"


class TestCopilotInstructions:
    """Verify copilot-instructions.md is correct and complete."""

    def test_copilot_instructions_exists(self) -> None:
        assert COPILOT_INSTRUCTIONS.exists(), "copilot-instructions.md is missing"

    def test_copilot_instructions_lists_all_tools(self) -> None:
        content = COPILOT_INSTRUCTIONS.read_text(encoding="utf-8")
        for tool in REGISTERED_TOOLS:
            assert tool in content, (
                f"copilot-instructions.md missing tool: {tool}"
            )

    def test_copilot_instructions_under_token_limit(self) -> None:
        content = COPILOT_INSTRUCTIONS.read_text(encoding="utf-8")
        # Rough estimate: 1 token ≈ 4 characters. Limit is 15,000 tokens.
        estimated_tokens = len(content) / 4
        assert estimated_tokens < 15_000, (
            f"copilot-instructions.md too large: ~{estimated_tokens:.0f} tokens (limit: 15,000)"
        )

    def test_copilot_instructions_has_cross_mcp_section(self) -> None:
        content = COPILOT_INSTRUCTIONS.read_text(encoding="utf-8")
        assert "Cross-MCP Collaboration" in content
        assert "HiveMind" in content
        assert "Sherlock" in content


class TestClaudeMd:
    """Verify CLAUDE.md exists and has required sections."""

    def test_claude_md_exists(self) -> None:
        assert CLAUDE_MD.exists(), "CLAUDE.md is missing"

    def test_claude_md_has_architecture(self) -> None:
        content = CLAUDE_MD.read_text(encoding="utf-8")
        assert "Architecture" in content

    def test_claude_md_has_rules(self) -> None:
        content = CLAUDE_MD.read_text(encoding="utf-8")
        assert "Rules" in content
        assert "read-only" in content.lower()
