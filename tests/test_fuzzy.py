"""Tests for core/fuzzy.py — fuzzy name resolution."""

from __future__ import annotations

import pytest

from core.fuzzy import FuzzyMatch, fuzzy_match, fuzzy_suggest


# ------------------------------------------------------------------
# Test data
# ------------------------------------------------------------------

PIPELINE_NAMES = [
    "ci_cd_AD",
    "cd_deploy_env",
    "ci_build_gateway",
    "ci_build_frontend",
    "cd_deploy_staging",
    "cd_deploy_production",
    "Build_Gateway_Service",
    "nightly_smoke_tests",
]


# ------------------------------------------------------------------
# fuzzy_match — exact
# ------------------------------------------------------------------


class TestFuzzyMatchExact:
    def test_exact_match(self) -> None:
        result = fuzzy_match("ci_cd_AD", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "ci_cd_AD"
        assert result.score == 1.0

    def test_exact_match_case_insensitive(self) -> None:
        result = fuzzy_match("CI_CD_AD", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "ci_cd_AD"
        assert result.score == 1.0

    def test_exact_match_with_mixed_case(self) -> None:
        result = fuzzy_match("build_gateway_service", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "Build_Gateway_Service"
        assert result.score == 1.0


# ------------------------------------------------------------------
# fuzzy_match — substring
# ------------------------------------------------------------------


class TestFuzzyMatchSubstring:
    def test_substring_match(self) -> None:
        result = fuzzy_match("deploy_env", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "cd_deploy_env"
        assert result.score > 0.5

    def test_short_substring(self) -> None:
        result = fuzzy_match("gateway", PIPELINE_NAMES)
        assert result is not None
        assert result.name in ("ci_build_gateway", "Build_Gateway_Service")
        assert result.score > 0.5

    def test_starts_with(self) -> None:
        result = fuzzy_match("ci_cd", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "ci_cd_AD"
        assert result.score >= 0.9


# ------------------------------------------------------------------
# fuzzy_match — partial / token
# ------------------------------------------------------------------


class TestFuzzyMatchPartial:
    def test_token_match(self) -> None:
        result = fuzzy_match("deploy staging", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "cd_deploy_staging"
        assert result.score > 0.5

    def test_partial_with_score(self) -> None:
        result = fuzzy_match("smoke", PIPELINE_NAMES)
        assert result is not None
        assert result.name == "nightly_smoke_tests"
        assert 0.5 < result.score < 1.0


# ------------------------------------------------------------------
# fuzzy_match — no match
# ------------------------------------------------------------------


class TestFuzzyMatchNoMatch:
    def test_no_match_returns_none(self) -> None:
        result = fuzzy_match("zzz_nonexistent_zzz", PIPELINE_NAMES)
        assert result is None

    def test_empty_query(self) -> None:
        assert fuzzy_match("", PIPELINE_NAMES) is None

    def test_empty_candidates(self) -> None:
        assert fuzzy_match("deploy", []) is None

    def test_both_empty(self) -> None:
        assert fuzzy_match("", []) is None

    def test_whitespace_query(self) -> None:
        assert fuzzy_match("   ", PIPELINE_NAMES) is None


# ------------------------------------------------------------------
# fuzzy_match — special characters
# ------------------------------------------------------------------


class TestFuzzyMatchSpecialChars:
    def test_match_with_underscores(self) -> None:
        result = fuzzy_match("ci_build", PIPELINE_NAMES)
        assert result is not None
        assert result.name.startswith("ci_build")

    def test_match_with_mixed_separators(self) -> None:
        result = fuzzy_match("deploy-env", ["cd_deploy_env", "cd_deploy_staging"])
        assert result is not None
        assert result.name == "cd_deploy_env"
        assert result.score > 0.5


# ------------------------------------------------------------------
# fuzzy_suggest
# ------------------------------------------------------------------


class TestFuzzySuggest:
    def test_suggestions_sorted_by_score(self) -> None:
        results = fuzzy_suggest("deploy", PIPELINE_NAMES)
        assert len(results) > 0
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_max_suggestions_respected(self) -> None:
        results = fuzzy_suggest("cd", PIPELINE_NAMES, max_suggestions=2)
        assert len(results) <= 2

    def test_suggestions_include_close_matches(self) -> None:
        results = fuzzy_suggest("deploy", PIPELINE_NAMES)
        names = [r.name for r in results]
        assert any("deploy" in n for n in names)

    def test_empty_query_returns_empty(self) -> None:
        assert fuzzy_suggest("", PIPELINE_NAMES) == []

    def test_empty_candidates_returns_empty(self) -> None:
        assert fuzzy_suggest("deploy", []) == []
