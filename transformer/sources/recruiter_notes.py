"""Parser for recruiter notes (plain-text `.txt`).

The project's one unstructured source. Recruiter notes are free text, so values
are pulled out with conservative, rule-based patterns and stamped with
`ExtractionMethod.TEXT_EXTRACTION`, which the confidence stage scores below
structured fields. The parser only extracts what free text yields reliably:
emails, phone-like strings, URLs, and explicitly labeled fields
("Name:", "Skills:"). It intentionally does not guess names or parse prose,
since a wrong extraction is worse than a missing one.

It does not normalize values; that is the normalization stage's job. One notes
file describes one candidate and yields at most one `RawRecord`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from transformer.errors import ParsingError
from transformer.models import ExtractionMethod, SourceType
from transformer.sources.base import RawRecord, SourceParser

# Conservative patterns. They aim for precision over recall: better to miss a
# value than to feed a wrong one into conflict resolution.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Phone-like: optional leading +, then 7+ digits possibly broken by space/-/().
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# "Label: value" lines we trust because the recruiter stated them explicitly.
# Maps a lowercased label to the canonical raw-field key used by all sources.
_LABEL_TO_FIELD: dict[str, str] = {
    "name": "full_name",
    "candidate": "full_name",
    "skills": "skills",
    "location": "location",
    "country": "country",
    "company": "company",
    "current company": "company",
}

_LABEL_RE = re.compile(r"^\s*([A-Za-z ]{2,20})\s*:\s*(.+?)\s*$")
_SKILL_DELIMITER = ","


class RecruiterNotesParser(SourceParser):
    """Parses a plain-text recruiter notes file into a single `RawRecord`."""

    source_type: SourceType = SourceType.RECRUITER_NOTES
    extraction_method: ExtractionMethod = ExtractionMethod.TEXT_EXTRACTION

    def can_parse(self, path: Path) -> bool:
        """Return True for `.txt` files. Never raises for unsupported files."""
        return path.suffix.lower() == ".txt"

    def parse(self, path: Path) -> list[RawRecord]:
        """Parse recruiter notes into at most one `RawRecord`.

        Args:
            path: Path to the `.txt` notes file.

        Returns:
            A single-element list, or an empty list if no fields were found.

        Raises:
            ParsingError: If the file cannot be read or decoded as UTF-8.
        """
        text = self._read_text(path)
        fields = self._extract_fields(text)
        if not fields:
            # Notes with no recognizable candidate data carry no signal.
            return []
        record = RawRecord(
            source=self.source_type,
            method=self.extraction_method,
            fields=fields,
            source_timestamp=None,  # free-text notes carry no reliable timestamp
            origin=str(path),
        )
        return [record]

    @staticmethod
    def _read_text(path: Path) -> str:
        """Read the notes file as UTF-8. IO/decoding failures are fatal."""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ParsingError(f"Cannot read notes file '{path}': {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ParsingError(
                f"Notes file '{path}' is not valid UTF-8: {exc}"
            ) from exc

    def _extract_fields(self, text: str) -> dict[str, Any]:
        """Pull recognizable fields out of free text.

        Labeled lines are applied first (highest trust), then pattern matches
        for contact details are merged in. Labeled values win on conflict so an
        explicit "Skills:" line is preferred over an incidental match.
        """
        fields: dict[str, Any] = {}

        contacts = self._extract_contacts(text)
        if contacts:
            fields.update(contacts)

        # Labeled lines override pattern matches for the same key.
        fields.update(self._extract_labeled(text))
        return fields

    def _extract_contacts(self, text: str) -> dict[str, Any]:
        """Extract emails, phones, and links as de-duplicated raw lists."""
        contacts: dict[str, Any] = {}
        emails = self._unique(_EMAIL_RE.findall(text))
        if emails:
            contacts["emails"] = emails

        # Exclude anything already captured as a URL to avoid phone false hits.
        urls = self._unique(_URL_RE.findall(text))
        phone_candidates = _PHONE_RE.findall(text)
        phones = self._unique(p.strip() for p in phone_candidates)
        if phones:
            contacts["phones"] = phones
        if urls:
            contacts["links"] = urls
        return contacts

    def _extract_labeled(self, text: str) -> dict[str, Any]:
        """Extract trusted "Label: value" lines into canonical raw keys."""
        result: dict[str, Any] = {}
        for line in text.splitlines():
            match = _LABEL_RE.match(line)
            if not match:
                continue
            label = match.group(1).strip().lower()
            value = match.group(2).strip()
            field = _LABEL_TO_FIELD.get(label)
            if not field or not value:
                continue
            if field == "skills":
                result[field] = self._split_skills(value)
            else:
                result[field] = value
        return result

    @staticmethod
    def _split_skills(value: str) -> list[str]:
        """Split a labeled skills line into trimmed, non-empty raw strings."""
        parts = (p.strip() for p in value.split(_SKILL_DELIMITER))
        return [p for p in parts if p]

    @staticmethod
    def _unique(values: Any) -> list[str]:
        """De-duplicate while preserving first-seen order (deterministic)."""
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result
