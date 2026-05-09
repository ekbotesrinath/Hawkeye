"""Tests for providers.harness.parser.truncate_to_tail."""

from __future__ import annotations

from providers.harness.parser import truncate_to_tail


class TestTruncateToTail:
    def test_short_logs_returned_unchanged(self) -> None:
        """Fewer than max_lines → return all."""
        raw = "\n".join(f"line {i}" for i in range(50))
        result = truncate_to_tail(raw, max_lines=100)
        assert result == raw

    def test_long_logs_return_last_100(self) -> None:
        """200 lines → return last 100 with header."""
        raw = "\n".join(f"line {i}" for i in range(200))
        result = truncate_to_tail(raw, max_lines=100)
        assert "100 lines above omitted" in result
        assert "showing last 100" in result
        assert "line 199" in result
        assert "line 100" in result
        # First lines should NOT be present
        assert "line 0\n" not in result

    def test_exactly_max_lines_no_header(self) -> None:
        """Exactly 100 lines → return all, no header."""
        raw = "\n".join(f"line {i}" for i in range(100))
        result = truncate_to_tail(raw, max_lines=100)
        assert result == raw
        assert "omitted" not in result

    def test_empty_string(self) -> None:
        """Empty input → return empty."""
        result = truncate_to_tail("", max_lines=100)
        assert result == ""

    def test_header_shows_correct_omitted_count(self) -> None:
        """Header should show exact count of omitted lines."""
        raw = "\n".join(f"line {i}" for i in range(350))
        result = truncate_to_tail(raw, max_lines=100)
        assert "250 lines above omitted" in result
        assert "showing last 100" in result

    def test_custom_max_lines(self) -> None:
        """Custom max_lines parameter is respected."""
        raw = "\n".join(f"line {i}" for i in range(50))
        result = truncate_to_tail(raw, max_lines=10)
        assert "40 lines above omitted" in result
        assert "line 49" in result

    def test_single_line_input(self) -> None:
        """Single line → return unchanged."""
        result = truncate_to_tail("single line", max_lines=100)
        assert result == "single line"
