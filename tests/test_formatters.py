"""Tests for core/formatters.py — shared formatting utilities."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from core.formatters import (
    format_duration,
    format_execution_table,
    format_relative_time,
    format_status_icon,
    truncate_logs,
)
from core.models import ExecutionStatus, PipelineExecution, StageExecution

_NOW_MS = int(time.time() * 1000)


# ------------------------------------------------------------------
# format_duration
# ------------------------------------------------------------------


class TestFormatDuration:
    def test_seconds_only(self) -> None:
        assert format_duration(_NOW_MS - 14_000, _NOW_MS) == "14s"

    def test_minutes_and_seconds(self) -> None:
        assert format_duration(_NOW_MS - 130_000, _NOW_MS) == "2m 10s"

    def test_hours_minutes_seconds(self) -> None:
        assert format_duration(_NOW_MS - 3_922_000, _NOW_MS) == "1h 5m 22s"

    def test_zero_duration(self) -> None:
        assert format_duration(_NOW_MS, _NOW_MS) == "0s"

    def test_missing_start(self) -> None:
        assert format_duration(None, _NOW_MS) == "—"

    def test_missing_end_uses_now(self) -> None:
        # When end is None the function uses wall-clock time
        result = format_duration(_NOW_MS - 5_000, None)
        # Should be ~5s, but allow for test runner jitter
        assert result.endswith("s")
        assert result != "—"

    def test_negative_duration(self) -> None:
        assert format_duration(_NOW_MS + 10_000, _NOW_MS) == "—"


# ------------------------------------------------------------------
# format_relative_time
# ------------------------------------------------------------------


class TestFormatRelativeTime:
    def test_none_timestamp(self) -> None:
        assert format_relative_time(None) == "—"

    def test_just_now(self) -> None:
        assert format_relative_time(_NOW_MS) == "just now"

    def test_minutes_ago(self) -> None:
        ts = _NOW_MS - 5 * 60 * 1000  # 5 min ago
        assert format_relative_time(ts) == "5m ago"

    def test_hours_ago(self) -> None:
        ts = _NOW_MS - 3 * 3600 * 1000  # 3h ago
        assert format_relative_time(ts) == "3h ago"

    def test_days_ago(self) -> None:
        ts = _NOW_MS - 2 * 86400 * 1000  # 2d ago
        assert format_relative_time(ts) == "2d ago"

    def test_future_timestamp(self) -> None:
        ts = _NOW_MS + 60_000
        assert format_relative_time(ts) == "just now"


# ------------------------------------------------------------------
# format_status_icon
# ------------------------------------------------------------------


class TestFormatStatusIcon:
    def test_success(self) -> None:
        assert format_status_icon(ExecutionStatus.SUCCESS) == "✅"

    def test_failed(self) -> None:
        assert format_status_icon(ExecutionStatus.FAILED) == "❌"

    def test_running(self) -> None:
        assert format_status_icon(ExecutionStatus.RUNNING) == "🔄"

    def test_paused(self) -> None:
        assert format_status_icon(ExecutionStatus.PAUSED) == "⏸️"

    def test_skipped(self) -> None:
        assert format_status_icon(ExecutionStatus.SKIPPED) == "⏭️"

    def test_unknown(self) -> None:
        assert format_status_icon(ExecutionStatus.UNKNOWN) == "❓"


# ------------------------------------------------------------------
# format_execution_table
# ------------------------------------------------------------------


def _make_execution(
    execution_id: str = "exec1",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    branch: str | None = "main",
    run_sequence: int = 100,
) -> PipelineExecution:
    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier="my_pipeline",
        pipeline_name="My Pipeline",
        status=status,
        start_ts=_NOW_MS - 60_000,
        end_ts=_NOW_MS,
        trigger_type="MANUAL",
        triggered_by="user@test.com",
        branch=branch,
        run_sequence=run_sequence,
        execution_url="https://app.harness.io/exec/1",
    )


class TestFormatExecutionTable:
    def test_empty_list(self) -> None:
        assert format_execution_table([]) == "No executions to display."

    def test_single_execution(self) -> None:
        result = format_execution_table([_make_execution()])
        assert "| #100 |" in result
        assert "My Pipeline" in result
        assert "✅" in result
        assert "[Link]" in result

    def test_multiple_executions(self) -> None:
        execs = [
            _make_execution(run_sequence=101, status=ExecutionStatus.SUCCESS),
            _make_execution(run_sequence=102, status=ExecutionStatus.FAILED),
        ]
        result = format_execution_table(execs)
        assert "| #101 |" in result
        assert "| #102 |" in result
        assert "✅" in result
        assert "❌" in result

    def test_no_branch(self) -> None:
        result = format_execution_table([_make_execution(branch=None)])
        assert "| — |" in result


# ------------------------------------------------------------------
# truncate_logs
# ------------------------------------------------------------------


class TestTruncateLogs:
    def test_short_logs_unchanged(self) -> None:
        text = "line1\nline2\nline3"
        assert truncate_logs(text, max_lines=10) == text

    def test_exact_limit_unchanged(self) -> None:
        text = "\n".join(f"line{i}" for i in range(200))
        assert truncate_logs(text, max_lines=200) == text

    def test_truncation_keeps_last_lines(self) -> None:
        lines = [f"line{i}" for i in range(300)]
        result = truncate_logs("\n".join(lines), max_lines=200)
        assert "100 lines truncated" in result
        assert "line299" in result
        assert "line0" not in result

    def test_custom_max_lines(self) -> None:
        lines = [f"line{i}" for i in range(50)]
        result = truncate_logs("\n".join(lines), max_lines=10)
        assert "40 lines truncated" in result
        assert "line49" in result
