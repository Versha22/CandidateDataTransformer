"""Entity resolution: group records belonging to the same candidate.

Per the Step 1 design, each batch may contain records for several candidates,
and this stage groups the records for one candidate together before any values
are merged. The rules, in order:

1. Exact identifier match first: records sharing a normalized email or phone are
   grouped. This is cheap and high-precision.
2. Fuzzy match only above a defined threshold: when records share no identifier,
   compare name plus employer/school overlap and group them only if the score
   clears a cut-off.
3. Ambiguous cases stay separate and are flagged: if the fuzzy score is in a
   borderline band, or names match while strong identifiers conflict, the
   records are NOT merged. A wrong merge is worse than a missed one.

This module decides *grouping* only. Choosing winning values within a group is
`merge.py`. It operates on normalized identifiers supplied by the caller so it
stays pure and independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from transformer.sources.base import RawRecord

# Fuzzy match bands (0-100). At or above MERGE we group; in the borderline band
# we flag for review and keep separate; below REVIEW we treat as different
# people. Tunable, like the confidence weights.
_FUZZY_MERGE_THRESHOLD: float = 88.0
_FUZZY_REVIEW_THRESHOLD: float = 75.0


@dataclass(frozen=True)
class ResolutionKey:
    """Normalized identity signals for one record, supplied by the pipeline.

    Keeping normalization out of this module makes resolution a pure function of
    its inputs and avoids duplicating the normalization rules.

    Attributes:
        emails: Normalized (lowercased) email addresses.
        phones: Normalized E.164 phone numbers.
        name: Normalized display name, used for fuzzy comparison.
        affiliations: Normalized company/school names for tie-breaking overlap.
    """

    emails: frozenset[str] = field(default_factory=frozenset)
    phones: frozenset[str] = field(default_factory=frozenset)
    name: str = ""
    affiliations: frozenset[str] = field(default_factory=frozenset)

    @property
    def strong_identifiers(self) -> frozenset[str]:
        """All exact-match keys (emails + phones) for this record."""
        return self.emails | self.phones


@dataclass
class CandidateGroup:
    """A set of records resolved to the same candidate.

    Attributes:
        record_indices: Indices into the batch's record list.
        needs_review: True if the group was formed by a borderline match and
            should be surfaced for human review rather than trusted outright.
    """

    record_indices: list[int] = field(default_factory=list)
    needs_review: bool = False


def resolve(
    records: list[RawRecord], keys: list[ResolutionKey]
) -> list[CandidateGroup]:
    """Group records that belong to the same candidate.

    Args:
        records: The batch's raw records.
        keys: One `ResolutionKey` per record, aligned by index with `records`.

    Returns:
        One `CandidateGroup` per distinct candidate, in a deterministic order.

    Raises:
        ValueError: If `records` and `keys` have different lengths.
    """
    if len(records) != len(keys):
        raise ValueError(
            f"records ({len(records)}) and keys ({len(keys)}) must align."
        )

    groups = _group_by_exact_identifier(keys)
    groups = _apply_fuzzy_pass(groups, keys)
    return groups


def _group_by_exact_identifier(keys: list[ResolutionKey]) -> list[CandidateGroup]:
    """Union records that share any normalized email or phone.

    Uses a simple union-find over identifier keys. High precision: two records
    are only joined here if they literally share a strong identifier.
    """
    parent: dict[int, int] = {i: i for i in range(len(keys))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    # Map each identifier to the first record index that owned it, then union.
    identifier_owner: dict[str, int] = {}
    for index, key in enumerate(keys):
        for identifier in key.strong_identifiers:
            if identifier in identifier_owner:
                union(index, identifier_owner[identifier])
            else:
                identifier_owner[identifier] = index

    return _materialize_groups(parent, find, len(keys))


def _materialize_groups(
    parent: dict[int, int], find, count: int
) -> list[CandidateGroup]:
    """Turn union-find roots into deterministic, index-sorted groups."""
    by_root: dict[int, list[int]] = {}
    for index in range(count):
        by_root.setdefault(find(index), []).append(index)

    groups = [
        CandidateGroup(record_indices=sorted(indices))
        for indices in by_root.values()
    ]
    # Deterministic order: sort groups by their smallest member index.
    groups.sort(key=lambda g: g.record_indices[0])
    return groups


def _apply_fuzzy_pass(
    groups: list[CandidateGroup], keys: list[ResolutionKey]
) -> list[CandidateGroup]:
    """Merge groups with no shared identifier when names strongly match.

    Compares every pair of groups that share no strong identifier. A score at or
    above the merge threshold joins them; a borderline score flags both as
    needs_review but keeps them separate; below the review threshold they remain
    distinct candidates.
    """
    result = list(groups)
    merged = True
    # Iterate to a fixed point so transitive merges (A~B, B~C) settle.
    while merged:
        merged = False
        for i in range(len(result)):
            for j in range(i + 1, len(result)):
                decision = _compare_groups(result[i], result[j], keys)
                if decision == "merge":
                    result[i] = _join(result[i], result[j])
                    del result[j]
                    merged = True
                    break
                if decision == "review":
                    result[i].needs_review = True
                    result[j].needs_review = True
            if merged:
                break
    result.sort(key=lambda g: g.record_indices[0])
    return result


def _compare_groups(
    a: CandidateGroup, b: CandidateGroup, keys: list[ResolutionKey]
) -> str:
    """Return 'merge', 'review', or 'distinct' for two groups.

    Groups that already share a strong identifier are not considered here (the
    exact pass would have joined them). If their identifiers actively conflict
    we never auto-merge; the best we do is flag for review on a strong name
    match, since same-name/different-contact may be two people.
    """
    best = _best_name_score(a, b, keys)
    if best < _FUZZY_REVIEW_THRESHOLD:
        return "distinct"

    conflicting_identifiers = _has_conflicting_identifiers(a, b, keys)
    if best >= _FUZZY_MERGE_THRESHOLD and not conflicting_identifiers:
        return "merge"
    # Strong name similarity but borderline, or identifiers disagree: be safe.
    return "review"


def _best_name_score(
    a: CandidateGroup, b: CandidateGroup, keys: list[ResolutionKey]
) -> float:
    """Highest name similarity (with affiliation boost) across group members."""
    best = 0.0
    for ia in a.record_indices:
        for ib in b.record_indices:
            name_a, name_b = keys[ia].name, keys[ib].name
            if not name_a or not name_b:
                continue
            score = fuzz.token_sort_ratio(name_a, name_b)
            if keys[ia].affiliations & keys[ib].affiliations:
                score = min(100.0, score + 5.0)  # shared employer/school boost
            best = max(best, score)
    return best


def _has_conflicting_identifiers(
    a: CandidateGroup, b: CandidateGroup, keys: list[ResolutionKey]
) -> bool:
    """True if both groups have strong identifiers and none are shared.

    Two records that each carry strong identifiers but share none are likely
    different people, so a name match alone must not merge them.
    """
    ids_a = _group_identifiers(a, keys)
    ids_b = _group_identifiers(b, keys)
    if not ids_a or not ids_b:
        return False
    return ids_a.isdisjoint(ids_b)


def _group_identifiers(
    group: CandidateGroup, keys: list[ResolutionKey]
) -> set[str]:
    """Union of strong identifiers across a group's records."""
    identifiers: set[str] = set()
    for index in group.record_indices:
        identifiers |= keys[index].strong_identifiers
    return identifiers


def _join(a: CandidateGroup, b: CandidateGroup) -> CandidateGroup:
    """Merge two groups, preserving deterministic index order and review flag."""
    return CandidateGroup(
        record_indices=sorted(a.record_indices + b.record_indices),
        needs_review=a.needs_review or b.needs_review,
    )
