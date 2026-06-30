"""Confidence scoring.

Per the Step 1 design, each value's confidence is a heuristic, rule-based score:

* a base score from the source's reliability,
* multiplied by a factor for the extraction method (structured beats free text),
* then nudged up when the same value is corroborated by independent sources.

The weights are intentionally simple and centralized here so they are easy to
read, defend, and tune. Nothing is learned. Profile-level confidence is computed
from identity attributes (name, email, phone) only, so a missing portfolio link
does not drag a solid profile down.
"""

from __future__ import annotations

from transformer.models import ExtractionMethod, SourceType

# Base reliability per source (design: Resume/GitHub/ATS High, Notes Medium,
# Unknown Low). CSV is a structured export, treated as High like ATS.
_SOURCE_BASE: dict[SourceType, float] = {
    SourceType.ATS: 0.9,
    SourceType.CSV: 0.9,
    SourceType.RESUME: 0.85,
    SourceType.GITHUB: 0.85,
    SourceType.RECRUITER_NOTES: 0.6,
    SourceType.UNKNOWN: 0.3,
}

# A value read from a structured field is more trustworthy than one pulled out
# of free text. Derived values inherit from their inputs, so they sit in between.
_METHOD_FACTOR: dict[ExtractionMethod, float] = {
    ExtractionMethod.STRUCTURED_FIELD: 1.0,
    ExtractionMethod.DERIVED: 0.9,
    ExtractionMethod.TEXT_EXTRACTION: 0.8,
}

# Each additional independent source agreeing on a value adds this much, capped
# so corroboration can raise confidence without ever exceeding 1.0.
_CORROBORATION_STEP: float = 0.05
_MAX_CONFIDENCE: float = 1.0

# Identity attributes used for the profile-level score (design decision).
_IDENTITY_FIELDS: tuple[str, ...] = ("full_name", "emails", "phones")

_DEFAULT_BASE = _SOURCE_BASE[SourceType.UNKNOWN]


def base_confidence(source: SourceType, method: ExtractionMethod) -> float:
    """Confidence for a single value from one source, before corroboration.

    Args:
        source: The originating source.
        method: How the value was extracted.

    Returns:
        A score in [0.0, 1.0] = source_base * method_factor.
    """
    base = _SOURCE_BASE.get(source, _DEFAULT_BASE)
    factor = _METHOD_FACTOR.get(method, _METHOD_FACTOR[ExtractionMethod.DERIVED])
    return _clamp(base * factor)


def corroborated_confidence(base_scores: list[float]) -> float:
    """Combine the per-source scores for one agreed-upon value.

    The strongest single source sets the floor; each additional agreeing source
    adds a small, capped bonus. This rewards independent agreement without
    letting many weak sources outweigh one strong one.

    Args:
        base_scores: Base confidence of each source that reported this value.

    Returns:
        The corroborated confidence in [0.0, 1.0].
    """
    if not base_scores:
        return 0.0
    strongest = max(base_scores)
    bonus = _CORROBORATION_STEP * (len(base_scores) - 1)
    return _clamp(strongest + bonus)


def profile_confidence(field_scores: dict[str, list[float]]) -> float | None:
    """Compute the profile-level score from identity attributes only.

    Args:
        field_scores: Map of canonical field name -> confidence scores of the
            values present for that field.

    Returns:
        The mean confidence across identity fields that have values, or None if
        no identity attribute is present at all.
    """
    present: list[float] = []
    for field in _IDENTITY_FIELDS:
        scores = field_scores.get(field) or []
        if scores:
            present.append(max(scores))
    if not present:
        return None
    return _clamp(sum(present) / len(present))


def _clamp(value: float) -> float:
    """Constrain a score to the valid [0.0, 1.0] range."""
    return max(0.0, min(_MAX_CONFIDENCE, value))
