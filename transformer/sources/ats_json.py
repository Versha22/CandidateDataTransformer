"""Parser for ATS (Applicant Tracking System) JSON exports.

This is a structured source: fields arrive under known keys, so extraction is a
direct mapping into a `RawRecord` with `ExtractionMethod.STRUCTURED_FIELD`. The
parser accepts either a single candidate object or a list of candidate objects
in one file, and tolerates a top-level wrapper key ("candidates") commonly seen
in ATS dumps.

`can_parse` is content-aware: it claims a `.json` file only when the content
looks like an ATS export (a candidate object, or a wrapper whose list holds
objects). This leaves room for other JSON-based sources to be added later
without this parser greedily claiming every `.json` file.

It does not normalize values (phones, dates, skills): that is the job of the
normalization stage. Its only responsibilities are reading the file, shaping
each candidate into a `RawRecord`, and failing loudly on malformed input so the
pipeline can quarantine it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from transformer.errors import ParsingError
from transformer.models import ExtractionMethod, SourceType
from transformer.sources.base import RawRecord, SourceParser, parse_source_timestamp

# Keys we treat as the record's production time, in priority order. Captured as
# source metadata (feeds provenance and the recency tie-breaker), not normalized.
_TIMESTAMP_KEYS: tuple[str, ...] = ("updated_at", "modified_at", "created_at")

# Top-level keys under which an ATS export may nest its candidate list.
_WRAPPER_KEYS: tuple[str, ...] = ("candidates", "records", "data")

# Fields that distinguish an ATS candidate object from arbitrary JSON. Presence
# of any one is enough to claim the file.
_CANDIDATE_MARKERS: tuple[str, ...] = (
    "full_name",
    "name",
    "email",
    "emails",
)


class AtsJsonParser(SourceParser):
    """Parses ATS JSON files into `RawRecord`s."""

    source_type: SourceType = SourceType.ATS
    extraction_method: ExtractionMethod = ExtractionMethod.STRUCTURED_FIELD

    def can_parse(self, path: Path) -> bool:
        """Return True only for `.json` files that look like an ATS export.

        Content-aware so future JSON sources are not greedily claimed. Never
        raises: an unreadable or non-matching file simply returns False, and a
        genuinely malformed ATS file is rejected later in `parse`.
        """
        if path.suffix.lower() != ".json":
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return self._looks_like_ats(data)

    def _looks_like_ats(self, data: Any) -> bool:
        """Heuristic: does this JSON contain ATS candidate object(s)?"""
        if isinstance(data, dict):
            for key in _WRAPPER_KEYS:
                items = data.get(key)
                if isinstance(items, list):
                    return self._is_candidate_object(items[0]) if items else True
            return self._is_candidate_object(data)
        if isinstance(data, list):
            return self._is_candidate_object(data[0]) if data else False
        return False

    @staticmethod
    def _is_candidate_object(obj: Any) -> bool:
        """True if `obj` is a dict carrying at least one candidate marker."""
        return isinstance(obj, dict) and any(
            marker in obj for marker in _CANDIDATE_MARKERS
        )

    def parse(self, path: Path) -> list[RawRecord]:
        """Parse an ATS JSON file into one `RawRecord` per candidate.

        Args:
            path: Path to the ATS JSON file.

        Returns:
            One `RawRecord` per candidate object found in the file.

        Raises:
            ParsingError: If the file cannot be read, is not valid JSON, or has
                a shape that does not contain candidate objects.
        """
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ParsingError(f"Cannot read ATS file '{path}': {exc}") from exc

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ParsingError(
                f"ATS file '{path}' is not valid JSON: {exc}"
            ) from exc

        candidates = self._extract_candidate_objects(data, path)
        return [self._to_raw_record(candidate, path) for candidate in candidates]

    def _extract_candidate_objects(
        self, data: Any, path: Path
    ) -> list[dict[str, Any]]:
        """Normalize the file's top-level shape into a list of candidate dicts.

        Accepts a single object, a list of objects, or an object that wraps the
        list under a known key. Anything else is malformed.
        """
        if isinstance(data, dict):
            for key in _WRAPPER_KEYS:
                if isinstance(data.get(key), list):
                    return self._require_object_list(data[key], path)
            return [data]

        if isinstance(data, list):
            return self._require_object_list(data, path)

        raise ParsingError(
            f"ATS file '{path}' must contain a candidate object or a list of "
            f"candidate objects, got {type(data).__name__}."
        )

    @staticmethod
    def _require_object_list(items: list[Any], path: Path) -> list[dict[str, Any]]:
        """Ensure every element is a JSON object; otherwise the file is bad."""
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ParsingError(
                    f"ATS file '{path}' candidate at index {index} must be an "
                    f"object, got {type(item).__name__}."
                )
        return items

    def _to_raw_record(self, candidate: dict[str, Any], path: Path) -> RawRecord:
        """Shape one candidate object into a `RawRecord`.

        All fields are passed through untouched into `fields`; the timestamp is
        lifted into `source_timestamp` for provenance via the shared helper.
        """
        return RawRecord(
            source=self.source_type,
            method=self.extraction_method,
            fields=candidate,
            source_timestamp=parse_source_timestamp(candidate, _TIMESTAMP_KEYS),
            origin=str(path),
        )
