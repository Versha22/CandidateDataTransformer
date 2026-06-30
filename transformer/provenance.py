"""Provenance construction.

Per the Step 1 design, every canonical value records where it came from: for
each contributing source it stores source, extraction method, confidence,
timestamp, and the raw value. Values that lose a conflict are kept and marked
`superseded` so a merge can be explained and replayed without re-ingesting.

This module builds `Provenance` and `Attribute` objects from raw inputs and
confidence scores; it does not decide winners (that is `merge.py`). It is the
single place that assembles the audit trail, so the format stays consistent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from transformer import confidence as confidence_rules
from transformer.models import (
    Attribute,
    ExtractionMethod,
    Provenance,
    SourceType,
)


def make_provenance(
    source: SourceType,
    method: ExtractionMethod,
    raw_value: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    superseded: bool = False,
) -> Provenance:
    """Build one provenance entry, scoring its base confidence consistently.

    The entry's confidence is the value's base confidence for this source and
    method (before corroboration), so a single provenance entry is always
    self-describing.

    Args:
        source: Originating source of the value.
        method: How the value was extracted.
        raw_value: Original value before normalization, kept for audit.
        timestamp: When the source record was produced, if known.
        superseded: True if this value lost a conflict but is kept for audit.

    Returns:
        A populated `Provenance` entry.
    """
    return Provenance(
        source=source,
        method=method,
        confidence=confidence_rules.base_confidence(source, method),
        raw_value=raw_value,
        timestamp=timestamp,
        superseded=superseded,
    )


def make_attribute(value: str, provenance: list[Provenance]) -> Attribute[str]:
    """Wrap a normalized value with its provenance and corroborated confidence.

    The value's confidence is derived from the non-superseded provenance entries
    (the sources that actually agree on this value). Superseded entries are kept
    for audit but do not raise the score.

    Args:
        value: The normalized canonical value.
        provenance: All provenance entries for this value (winners and losers).

    Returns:
        A fully-populated `Attribute`.

    Raises:
        ValueError: If no provenance is supplied; every canonical value must be
            attributable to at least one source.
    """
    if not provenance:
        raise ValueError("An attribute must have at least one provenance entry.")

    contributing = [p.confidence for p in provenance if not p.superseded]
    score = confidence_rules.corroborated_confidence(
        contributing or [p.confidence for p in provenance]
    )
    return Attribute(value=value, confidence=score, provenance=provenance)
