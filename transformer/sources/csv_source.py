"""Parser for candidate CSV files.

A structured source where each row is one candidate and each column is a field.
Values arrive as flat strings, so this parser does two shaping jobs (and only
these): it drops empty cells so "missing" stays distinct from "empty" downstream,
and it splits known multi-value columns (e.g. skills) into lists. It performs no
normalization (phones, dates, country, canonical skill names): that is the job
of the normalization stage.

Malformed input (unreadable file, undecodable bytes) raises `ParsingError` so
the pipeline can quarantine it. A structurally valid but empty or header-only
file is not an error and yields no records.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from transformer.errors import ParsingError
from transformer.models import ExtractionMethod, SourceType
from transformer.sources.base import RawRecord, SourceParser, parse_source_timestamp

# Timestamp columns, in priority order (shared semantics with the ATS parser).
_TIMESTAMP_KEYS: tuple[str, ...] = ("updated_at", "modified_at", "created_at")

# Columns whose cell holds several values separated by a delimiter. Splitting is
# shaping, not normalization: we only turn one string into a list of raw strings.
_MULTI_VALUE_COLUMNS: frozenset[str] = frozenset({"skills", "emails", "phones"})

# Delimiter used inside multi-value cells. Comma is most common in candidate
# CSV exports for skills/emails; the CSV field delimiter itself stays the default.
_MULTI_VALUE_DELIMITER: str = ","


class CsvParser(SourceParser):
    """Parses candidate CSV files into `RawRecord`s, one per row."""

    source_type: SourceType = SourceType.CSV
    extraction_method: ExtractionMethod = ExtractionMethod.STRUCTURED_FIELD

    def can_parse(self, path: Path) -> bool:
        """Return True for `.csv` files. Never raises for unsupported files."""
        return path.suffix.lower() == ".csv"

    def parse(self, path: Path) -> list[RawRecord]:
        """Parse a CSV file into one `RawRecord` per non-empty row.

        Args:
            path: Path to the CSV file.

        Returns:
            One `RawRecord` per data row. Empty or header-only files yield [].

        Raises:
            ParsingError: If the file cannot be read or decoded.
        """
        rows = self._read_rows(path)
        records: list[RawRecord] = []
        for row in rows:
            fields = self._shape_row(row)
            if not fields:
                # A blank line / all-empty row carries no candidate data.
                continue
            records.append(self._to_raw_record(fields, path))
        return records

    @staticmethod
    def _read_rows(path: Path) -> list[dict[str, Any]]:
        """Read the CSV into a list of column->value dicts.

        Opened as UTF-8 with `newline=""` per the csv module's documented
        contract. Decoding or IO failures are fatal for this file.
        """
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                return list(csv.DictReader(handle))
        except OSError as exc:
            raise ParsingError(f"Cannot read CSV file '{path}': {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ParsingError(
                f"CSV file '{path}' is not valid UTF-8: {exc}"
            ) from exc

    def _shape_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Turn one raw CSV row into clean raw fields.

        - Strips whitespace and drops empty cells so absence stays explicit.
        - Splits known multi-value columns into lists of raw strings.
        - Ignores `DictReader`'s overflow key (None) from ragged rows.
        """
        shaped: dict[str, Any] = {}
        for column, value in row.items():
            if column is None:
                # Extra cells beyond the header (ragged row); not a real field.
                continue
            cleaned = self._clean(value)
            if cleaned is None:
                continue
            if column in _MULTI_VALUE_COLUMNS:
                shaped[column] = self._split_multi_value(cleaned)
            else:
                shaped[column] = cleaned
        return shaped

    @staticmethod
    def _clean(value: Any) -> str | None:
        """Trim a cell to a non-empty string, or None if effectively empty."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _split_multi_value(value: str) -> list[str]:
        """Split a multi-value cell into trimmed, non-empty raw strings."""
        parts = (part.strip() for part in value.split(_MULTI_VALUE_DELIMITER))
        return [part for part in parts if part]

    def _to_raw_record(self, fields: dict[str, Any], path: Path) -> RawRecord:
        """Shape cleaned fields into a `RawRecord` with source metadata."""
        return RawRecord(
            source=self.source_type,
            method=self.extraction_method,
            fields=fields,
            source_timestamp=parse_source_timestamp(fields, _TIMESTAMP_KEYS),
            origin=str(path),
        )
