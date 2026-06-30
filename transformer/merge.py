"""Conflict resolution: collapse a candidate's records into one profile.

Per the Step 1 design, once entity resolution has grouped the records for one
candidate, this stage produces a single canonical `CandidateProfile`:

* Scalar fields (full_name): the winning value is the one with the highest
  `confidence x source_tier`, ties broken by the most recent source timestamp.
  Losing values are retained in provenance and marked `superseded`.
* List fields (emails, phones, skills): values are de-duplicated on their
  normalized form; each surviving value keeps the provenance of every source
  that reported it, so corroboration raises its confidence.
* Record fields (experience, education, links): collected and de-duplicated by
  identity, since these are additive rather than conflicting.

Source-tier ordering is configurable; the default is ATS >= CSV >= Resume >=
GitHub >= Notes. Merge consumes pre-normalized `MappedRecord`s so it stays a
pure function of its inputs and re-uses no normalization logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from transformer import confidence as confidence_rules
from transformer.errors import MergeError
from transformer.models import (
    Attribute,
    CandidateProfile,
    Education,
    Experience,
    Link,
    LinkType,
    Name,
    Provenance,
    SourceType,
)
from transformer.provenance import make_provenance

# Default source tier weights (design: ATS >= Resume >= GitHub >= Notes; CSV is
# a structured export, weighted like ATS). Higher wins. Overridable via config.
DEFAULT_SOURCE_TIER: dict[SourceType, float] = {
    SourceType.ATS: 1.0,
    SourceType.CSV: 1.0,
    SourceType.RESUME: 0.8,
    SourceType.GITHUB: 0.7,
    SourceType.RECRUITER_NOTES: 0.5,
    SourceType.UNKNOWN: 0.2,
}

_DEFAULT_TIER = DEFAULT_SOURCE_TIER[SourceType.UNKNOWN]


@dataclass(frozen=True)
class ValueCandidate:
    """One normalized value reported by one source, with its metadata.

    Attributes:
        value: The normalized canonical value.
        provenance: The provenance entry for this source's report of it.
    """

    value: str
    provenance: Provenance


@dataclass
class MappedRecord:
    """A record's normalized canonical fields, ready for merging.

    Built by the pipeline from a `RawRecord` (normalization + provenance). Merge
    depends only on this shape, never on raw sources or `normalize.py`.

    Scalar fields hold at most one `ValueCandidate`; list/record fields hold
    several. All values are already normalized.
    """

    source: SourceType
    timestamp: Optional[datetime] = None
    full_name: Optional[ValueCandidate] = None
    emails: list[ValueCandidate] = field(default_factory=list)
    phones: list[ValueCandidate] = field(default_factory=list)
    skills: list[ValueCandidate] = field(default_factory=list)
    experience: list[Experience] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


def merge_records(
    candidate_id: str,
    records: list[MappedRecord],
    needs_review: bool = False,
    tier: dict[SourceType, float] | None = None,
) -> CandidateProfile:
    """Collapse one candidate's mapped records into a canonical profile.

    Args:
        candidate_id: Pre-assigned internal UUID for this candidate.
        records: The mapped records resolved to this candidate (non-empty).
        needs_review: Carried over from entity resolution for ambiguous groups.
        tier: Optional source-tier weights; defaults to DEFAULT_SOURCE_TIER.

    Returns:
        The merged `CandidateProfile`.

    Raises:
        MergeError: If no records are supplied; there is nothing to merge.
    """
    if not records:
        raise MergeError(f"No records to merge for candidate '{candidate_id}'.")
    weights = tier or DEFAULT_SOURCE_TIER

    full_name = _resolve_scalar_name(records, weights)
    emails = _merge_list_field([vc for r in records for vc in r.emails])
    phones = _merge_list_field([vc for r in records for vc in r.phones])
    skills = _merge_list_field([vc for r in records for vc in r.skills])

    profile = CandidateProfile(
        candidate_id=candidate_id,
        full_name=full_name,
        emails=emails,
        phones=phones,
        skills=skills,
        experience=_dedupe_experience(records),
        education=_dedupe_education(records),
        links=_dedupe_links(records),
        needs_review=needs_review,
    )
    profile.profile_confidence = _profile_confidence(profile)
    return profile


def _tier_weight(source: SourceType, weights: dict[SourceType, float]) -> float:
    """Return the configured tier weight for a source."""
    return weights.get(source, _DEFAULT_TIER)


def _resolve_scalar_name(
    records: list[MappedRecord], weights: dict[SourceType, float]
) -> Optional[Attribute[Name]]:
    """Pick the winning name; keep all others as superseded provenance.

    Winner = highest `confidence x tier_weight`, ties broken by most recent
    timestamp, then by source-tier as a final stable fallback.
    """
    candidates = [
        (r, r.full_name) for r in records if r.full_name is not None
    ]
    if not candidates:
        return None

    def sort_key(item: tuple[MappedRecord, ValueCandidate]) -> tuple:
        record, vc = item
        score = vc.provenance.confidence * _tier_weight(record.source, weights)
        recency = record.timestamp or datetime.min
        return (score, recency, _tier_weight(record.source, weights))

    ranked = sorted(candidates, key=sort_key, reverse=True)
    winner_record, winner_vc = ranked[0]

    provenance = [winner_vc.provenance]
    for record, vc in ranked[1:]:
        provenance.append(vc.provenance.model_copy(update={"superseded": True}))

    name = _parse_name(winner_vc.value)
    score = winner_vc.provenance.confidence
    return Attribute[Name](value=name, confidence=score, provenance=provenance)


def _merge_list_field(values: list[ValueCandidate]) -> list[Attribute[str]]:
    """De-duplicate list values on normalized form, merging provenance.

    Values reported by several sources collapse to one `Attribute` whose
    confidence reflects the corroboration. Output order is deterministic
    (first-seen normalized value).
    """
    order: list[str] = []
    grouped: dict[str, list[Provenance]] = {}
    for vc in values:
        if vc.value not in grouped:
            grouped[vc.value] = []
            order.append(vc.value)
        grouped[vc.value].append(vc.provenance)

    attributes: list[Attribute[str]] = []
    for value in order:
        provenance = grouped[value]
        score = confidence_rules.corroborated_confidence(
            [p.confidence for p in provenance]
        )
        attributes.append(
            Attribute[str](value=value, confidence=score, provenance=provenance)
        )
    return attributes


def _dedupe_experience(records: list[MappedRecord]) -> list[Experience]:
    """Collect experience entries, de-duplicating by (company, title)."""
    seen: set[tuple[str, str]] = set()
    result: list[Experience] = []
    for record in records:
        for exp in record.experience:
            key = (exp.company.value, exp.title.value if exp.title else "")
            if key not in seen:
                seen.add(key)
                result.append(exp)
    return result


def _dedupe_education(records: list[MappedRecord]) -> list[Education]:
    """Collect education entries, de-duplicating by (institution, degree)."""
    seen: set[tuple[str, str]] = set()
    result: list[Education] = []
    for record in records:
        for edu in record.education:
            key = (edu.institution.value, edu.degree.value if edu.degree else "")
            if key not in seen:
                seen.add(key)
                result.append(edu)
    return result


def _dedupe_links(records: list[MappedRecord]) -> list[Link]:
    """Collect links, de-duplicating by (type, url)."""
    seen: set[tuple[LinkType, str]] = set()
    result: list[Link] = []
    for record in records:
        for link in record.links:
            key = (link.type, link.url.value)
            if key not in seen:
                seen.add(key)
                result.append(link)
    return result


def _parse_name(display: str) -> Name:
    """Build a Name from a display string, splitting given/family heuristically.

    Names don't split reliably, so we only populate given/family for a simple
    two-token "Given Family" form and always keep the original display string.
    """
    tokens = display.split()
    if len(tokens) == 2:
        return Name(display=display, given=tokens[0], family=tokens[1])
    return Name(display=display)


def _profile_confidence(profile: CandidateProfile) -> float | None:
    """Compute the identity-only profile score from the merged attributes."""
    field_scores = {
        "full_name": [profile.full_name.confidence] if profile.full_name else [],
        "emails": [a.confidence for a in profile.emails],
        "phones": [a.confidence for a in profile.phones],
    }
    return confidence_rules.profile_confidence(field_scores)
