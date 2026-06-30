"""Parser for ATS (Applicant Tracking System) JSON exports.

This is a structured source: fields arrive under known keys, so extraction is a
direct mapping into a `RawRecord` with `ExtractionMethod.STRUCTURED_FIELD`. The
parser accepts either a single candidate object or a list of candidate objects
in one file, and tolerates a top-level wrapper key ("candidates") commonly seen
in ATS dumps.

It does not normalize values (phones, dates, skills): that is the job of the
normalization stage. Its only responsibilities are reading the file, shaping
each candidate into a `RawRecord`, and failing loudly on malformed input so the
pipeline can quarantine it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dateutil import parser as date_parser

from transformer.errors import ParsingError
from transformer.models import ExtractionMethod, SourceType
from transformer.sources.base import RawRecord, SourceParser

# Keys we treat as the record's production time, in priority order. The first
# one present and parseable wins. Captured here (not in normalize) because the
# timestamp is source metadata, used for provenance and recency tie-breaking.
_TIMESTAMP_KEYS: tuple[str, ...] = ("updated_at", "modified_at", "created_at")

# Top-level keys under which an ATS export may nest its candidate list.
_WRAPPER_KEYS: tuple[str, ...] = ("candidates", "records", "data")


class AtsJsonParser(SourceParser):
    """Parses ATS JSON files into `RawRecord`s."""

    source_type: SourceType = SourceType.ATS
    extraction_method: ExtractionMethod = ExtractionMethod.STRUCTURED_FIELD

    def can_parse(self, path: Path) -> bool:
        """Return True for `.json` files. Never raises for unsupported files."""
        return path.suffix.lower() == ".json"

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
        return [
            self._to_raw_record(candidate, path)
            for candidate in candidates
        ]

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
            # A bare object is treated as a single candidate.
            return [data]

        if isinstance(data, list):
            return self._require_object_list(data, path)

        raise ParsingError(
            f"ATS file '{path}' must contain a candidate object or a list of "
            f"candidate objects, got {type(data).__name__}."
        )

    @staticmethod
    def _require_object_list(
        items: list[Any], path: Path
    ) -> list[dict[str, Any]]:
        """Ensure every element is a JSON object; otherwise the file is bad."""
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ParsingError(
                    f"ATS file '{path}' candidate at index {index} must be an "
                    f"object, got {type(item).__name__}."
                )
        return items

    def _to_raw_record(
        self, candidate: dict[str, Any], path: Path
    ) -> RawRecord:
        """Shape one candidate object into a `RawRecord`.

        All known fields are passed through untouched into `fields`; the
        timestamp is lifted out into `source_timestamp` for provenance.
        """
        return RawRecord(
            source=self.source_type,
            method=self.extraction_method,
            fields=candidate,
            source_timestamp=self._extract_timestamp(candidate),
            origin=str(path),
        )

    @staticmethod
    def _extract_timestamp(candidate: dict[str, Any]) -> Optional[datetime]:
        """Return the record's production time if present and parseable.

        A missing or unparseable timestamp is not fatal: it simply means this
        record has no recency signal for tie-breaking, so we return None rather
        than raising.
        """
        for key in _TIMESTAMP_KEYS:
            value = candidate.get(key)
            if not value:
                continue
            try:
                return date_parser.parse(str(value))
            except (ValueError, OverflowError):
                # Malformed timestamp is non-fatal; keep looking, then give up.
                continue
        return None
