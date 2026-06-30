"""Pipeline orchestration.

Wires the stages into one batch run, following the Step 1 design order:

    detect -> parse -> normalize+map -> resolve -> merge -> project

Business logic lives in the stage modules; this file only sequences them and
moves typed data between them. Per-record failures are isolated here: a record
that cannot be mapped, or a group that cannot be merged, is quarantined and the
batch continues. A bad config or vocabulary is a startup failure handled by the
caller (CLI) before a pipeline is ever constructed.

The result is an output envelope (candidates + a quarantine report) that the CLI
serializes with `json.dump`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from transformer import normalize
from transformer.config import ProjectionConfig
from transformer.detect import InputDetector
from transformer.errors import QuarantineError, RecordError
from transformer.merge import MappedRecord, ValueCandidate, merge_records
from transformer.normalize import SkillVocabulary
from transformer.projection import project
from transformer.provenance import make_provenance
from transformer.resolution import ResolutionKey, resolve
from transformer.sources.base import RawRecord

# Raw-field keys shared by every source (the parser key contract). Listed here
# so the mapping from raw fields to canonical fields is explicit and in one spot.
_NAME_KEYS: tuple[str, ...] = ("full_name", "name")
_EMAIL_KEYS: tuple[str, ...] = ("emails", "email")
_PHONE_KEYS: tuple[str, ...] = ("phones", "phone")
_SKILL_KEYS: tuple[str, ...] = ("skills",)
_COMPANY_KEYS: tuple[str, ...] = ("company", "current_company")


@dataclass
class PipelineResult:
    """Outcome of a batch run.

    Attributes:
        candidates: Projected, JSON-ready candidate profiles.
        quarantined: Human-readable messages for inputs/records that were
            skipped, each carrying its cause.
    """

    candidates: list[dict[str, Any]] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)

    def to_envelope(self) -> dict[str, Any]:
        """Return the top-level output document."""
        return {
            "candidates": self.candidates,
            "quarantined": self.quarantined,
        }


class Pipeline:
    """Runs a full batch transformation end to end."""

    def __init__(
        self,
        config: ProjectionConfig,
        vocabulary: SkillVocabulary,
        detector: Optional[InputDetector] = None,
    ) -> None:
        """Create a pipeline.

        Args:
            config: Validated runtime projection config (loaded at startup).
            vocabulary: Loaded skill vocabulary (loaded at startup).
            detector: Input detector; defaults to a standard `InputDetector`.
        """
        self._config = config
        self._vocabulary = vocabulary
        self._detector = detector or InputDetector()

    def run(self, input_dir: Path) -> PipelineResult:
        """Execute the pipeline over every file in `input_dir`.

        Detection failures, per-record mapping failures, and per-group merge
        failures are each isolated into the quarantine report so a single bad
        input never stops the batch.
        """
        result = PipelineResult()

        detection = self._detector.parse_batch(input_dir)
        result.quarantined.extend(str(q) for q in detection.quarantined)

        # Map each raw record into normalized values + resolution keys, keeping
        # the two lists index-aligned so resolution and merge can index them.
        mapped: list[MappedRecord] = []
        keys: list[ResolutionKey] = []
        for record in detection.records:
            try:
                mapped_record = self._map_record(record)
                resolution_key = self._build_resolution_key(record, mapped_record)
            except RecordError as exc:
                result.quarantined.append(
                    str(
                        QuarantineError(
                            "Skipped record during mapping",
                            source=record.origin,
                            cause=exc,
                        )
                    )
                )
                continue
            mapped.append(mapped_record)
            keys.append(resolution_key)

        # `resolve` works purely off the keys; `mapped` stays index-aligned with
        # `keys`, so group.record_indices index straight into `mapped`.
        for group in resolve(keys):
            try:
                profile = merge_records(
                    candidate_id=str(uuid.uuid4()),
                    records=[mapped[i] for i in group.record_indices],
                    needs_review=group.needs_review,
                )
                result.candidates.append(project(profile, self._config))
            except RecordError as exc:
                indices = ", ".join(str(i) for i in group.record_indices)
                result.quarantined.append(
                    str(
                        QuarantineError(
                            f"Skipped candidate group (records {indices})",
                            cause=exc,
                        )
                    )
                )
        return result

    # --- Mapping: RawRecord -> MappedRecord (normalize + attach provenance) ---

    def _map_record(self, record: RawRecord) -> MappedRecord:
        """Normalize a raw record's fields into canonical value candidates."""
        return MappedRecord(
            source=record.source,
            timestamp=record.source_timestamp,
            full_name=self._map_name(record),
            emails=self._map_emails(record),
            phones=self._map_phones(record),
            skills=self._map_skills(record),
            # experience/education/links are supported by the canonical model
            # and by merge; mapping them from raw fields is out of v1 scope.
        )

    def _map_name(self, record: RawRecord) -> Optional[ValueCandidate]:
        # _first_str takes only the first value if a source supplies several
        # names; multi-name sources are out of v1 scope.
        raw = _first_str(record.fields, _NAME_KEYS)
        if not raw:
            return None
        return self._candidate(record, value=raw, raw=raw)

    def _map_emails(self, record: RawRecord) -> list[ValueCandidate]:
        candidates: list[ValueCandidate] = []
        for raw in _as_list(record.fields, _EMAIL_KEYS):
            normalized = normalize.normalize_email(raw)
            if normalized:
                candidates.append(self._candidate(record, normalized, raw))
        return candidates

    def _map_phones(self, record: RawRecord) -> list[ValueCandidate]:
        candidates: list[ValueCandidate] = []
        for raw in _as_list(record.fields, _PHONE_KEYS):
            normalized = normalize.normalize_phone(raw)
            if normalized:
                candidates.append(self._candidate(record, normalized, raw))
        return candidates

    def _map_skills(self, record: RawRecord) -> list[ValueCandidate]:
        candidates: list[ValueCandidate] = []
        for raw in _as_list(record.fields, _SKILL_KEYS):
            canonical = normalize.normalize_skill(raw, self._vocabulary)
            # Unmapped skills are kept as the trimmed raw value: keep the stated
            # skill rather than dropping it (design: keep raw / mark unmapped).
            value = canonical or raw.strip()
            if value:
                candidates.append(self._candidate(record, value, raw))
        return candidates

    def _candidate(
        self, record: RawRecord, value: str, raw: str
    ) -> ValueCandidate:
        """Pair a normalized value with a provenance entry for this source."""
        provenance = make_provenance(
            source=record.source,
            method=record.method,
            raw_value=raw,
            timestamp=record.source_timestamp,
        )
        return ValueCandidate(value=value, provenance=provenance)

    # --- Resolution key construction ---

    def _build_resolution_key(
        self, record: RawRecord, mapped: MappedRecord
    ) -> ResolutionKey:
        """Build the normalized identity signals used for entity resolution."""
        affiliations = {
            company.strip().lower()
            for company in _as_list(record.fields, _COMPANY_KEYS)
            if company and company.strip()
        }
        return ResolutionKey(
            emails=frozenset(vc.value for vc in mapped.emails),
            phones=frozenset(vc.value for vc in mapped.phones),
            name=mapped.full_name.value.lower() if mapped.full_name else "",
            affiliations=frozenset(affiliations),
        )


def _first_str(fields: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    """Return the first present field among `keys` as a trimmed string.

    Accepts either a scalar string or a list whose first element is a string,
    so the same helper works across structured and list-valued source fields.
    """
    for key in keys:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return None


def _as_list(fields: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Return values for the first present key as a list of raw strings."""
    for key in keys:
        value = fields.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            return [str(v) for v in value if v is not None and str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []
