"""Fuzzy name resolution for pipeline and stage identifiers.

No external dependencies — uses substring matching and a simple
edit-distance algorithm to find the best match.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FuzzyMatch:
    """Result of a fuzzy match attempt."""

    name: str
    score: float  # 0.0 – 1.0


def fuzzy_match(query: str, candidates: list[str], threshold: float = 0.5) -> FuzzyMatch | None:
    """Return the best match above *threshold*, or ``None``.

    Matching strategy (in priority order):
    1. Exact match (case-insensitive) → score 1.0
    2. Candidate starts with query → score 0.9
    3. Query is a substring of candidate → score based on length ratio
    4. All query tokens appear in candidate → score based on coverage
    5. Normalised edit-distance similarity → raw score
    """
    if not query or not candidates:
        return None

    query_lower = query.lower().strip()
    if not query_lower:
        return None

    best: FuzzyMatch | None = None

    for candidate in candidates:
        cand_lower = candidate.lower()

        # 1. Exact
        if query_lower == cand_lower:
            return FuzzyMatch(name=candidate, score=1.0)

        # 2. Starts-with
        if cand_lower.startswith(query_lower):
            score = 0.9
            if best is None or score > best.score:
                best = FuzzyMatch(name=candidate, score=score)
            continue

        # 3. Substring
        if query_lower in cand_lower:
            score = 0.6 + 0.3 * (len(query_lower) / len(cand_lower))
            if best is None or score > best.score:
                best = FuzzyMatch(name=candidate, score=score)
            continue

        # 4. Token overlap — split on common separators
        query_tokens = _tokenize(query_lower)
        cand_tokens = _tokenize(cand_lower)
        if query_tokens and cand_tokens:
            matched = sum(1 for qt in query_tokens if any(qt in ct for ct in cand_tokens))
            if matched == len(query_tokens):
                coverage = matched / max(len(cand_tokens), 1)
                score = 0.5 + 0.3 * coverage
                if best is None or score > best.score:
                    best = FuzzyMatch(name=candidate, score=score)
                continue

        # 5. Edit-distance similarity
        score = _similarity(query_lower, cand_lower)
        if best is None or score > best.score:
            best = FuzzyMatch(name=candidate, score=score)

    if best is not None and best.score >= threshold:
        return best
    return None


def fuzzy_suggest(query: str, candidates: list[str], max_suggestions: int = 5) -> list[FuzzyMatch]:
    """Return up to *max_suggestions* matches sorted by descending score.

    Unlike :func:`fuzzy_match`, this returns results even below the default
    threshold so the caller can present them as suggestions.
    """
    if not query or not candidates:
        return []

    scored: list[FuzzyMatch] = []
    query_lower = query.lower().strip()
    if not query_lower:
        return []

    for candidate in candidates:
        cand_lower = candidate.lower()

        if query_lower == cand_lower:
            score = 1.0
        elif cand_lower.startswith(query_lower):
            score = 0.9
        elif query_lower in cand_lower:
            score = 0.6 + 0.3 * (len(query_lower) / len(cand_lower))
        else:
            query_tokens = _tokenize(query_lower)
            cand_tokens = _tokenize(cand_lower)
            if query_tokens and cand_tokens:
                matched = sum(1 for qt in query_tokens if any(qt in ct for ct in cand_tokens))
                if matched == len(query_tokens):
                    coverage = matched / max(len(cand_tokens), 1)
                    score = 0.5 + 0.3 * coverage
                else:
                    score = _similarity(query_lower, cand_lower)
            else:
                score = _similarity(query_lower, cand_lower)

        if score > 0.0:
            scored.append(FuzzyMatch(name=candidate, score=score))

    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:max_suggestions]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Split on underscores, hyphens, spaces, and camelCase boundaries."""
    import re
    # Insert space before uppercase letters preceded by lowercase (camelCase split)
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Split on underscores, hyphens, spaces
    tokens = re.split(r"[_\-\s]+", spaced)
    return [t for t in tokens if t]


def _similarity(a: str, b: str) -> float:
    """Normalised edit-distance similarity between two strings (0.0–1.0)."""
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    dist = _levenshtein(a, b)
    return 1.0 - (dist / max_len)


def _levenshtein(a: str, b: str) -> int:
    """Compute the Levenshtein edit distance between *a* and *b*."""
    if len(a) < len(b):
        return _levenshtein(b, a)

    # Previous row of distances
    prev = list(range(len(b) + 1))

    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(
                curr[j] + 1,        # insert
                prev[j + 1] + 1,    # delete
                prev[j] + cost,     # replace
            ))
        prev = curr

    return prev[len(b)]
