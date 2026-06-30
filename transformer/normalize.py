"""Normalization stage: raw source values -> canonical normalized values.

Per the Step 1 design this stage applies the normalization rules:

* email   -> lowercased, trimmed (basic shape check)
* phones  -> E.164 (via `phonenumbers`)
* dates   -> ISO-8601, partial dates allowed (via `dateutil`)
* country -> ISO-3166 alpha-2 (via `pycountry`)
* skills  -> canonical names from a controlled vocabulary

Every rule is a small, pure function. A value that cannot be normalized is
*dropped* (returns None), not fatal: the record continues without that field,
consistent with the design's "keep the raw value / mark unmapped, don't fail the
batch" rule. The functions here return normalized strings only; wrapping them in
canonical `Attribute`s (with provenance and confidence) happens in later stages.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import phonenumbers
import pycountry
from dateutil import parser as date_parser
from dateutil.parser import ParserError

# A partial date like "2021" or "2021-03" is valid; we detect how much of it the
# source actually specified so we don't fabricate a day that wasn't given.
_ISO_FULL = "%Y-%m-%d"
_ISO_MONTH = "%Y-%m"
_ISO_YEAR = "%Y"

# Default region used when a phone number has no country code and the source
# gives no country. Kept explicit and overridable rather than hidden.
_DEFAULT_PHONE_REGION = "US"


class SkillVocabulary:
    """Maps raw skill mentions to canonical skill names.

    Backed by a mapping of the form ``{"canonical": ["alias", ...]}``. Lookups
    are case-insensitive. An unknown skill returns None so the caller can keep
    the raw value and mark it unmapped rather than guessing.
    """

    def __init__(self, alias_to_canonical: dict[str, str]) -> None:
        self._alias_to_canonical = alias_to_canonical

    @classmethod
    def from_dict(cls, mapping: dict[str, list[str]]) -> "SkillVocabulary":
        """Build a vocabulary from an in-memory canonical -> aliases mapping.

        Flattens canonical names and their aliases into a single lowercase
        lookup. The canonical name maps to itself so it is always recognized.

        Raises:
            ValueError: If the mapping is not the expected shape.
        """
        if not isinstance(mapping, dict):
            raise ValueError("Skills vocabulary must be a mapping.")

        alias_to_canonical: dict[str, str] = {}
        for canonical, aliases in mapping.items():
            alias_to_canonical[canonical.lower()] = canonical
            for alias in aliases or []:
                alias_to_canonical[str(alias).lower()] = canonical
        return cls(alias_to_canonical)

    @classmethod
    def from_file(cls, path: str | Path) -> "SkillVocabulary":
        """Load a vocabulary from a JSON file of canonical -> aliases.

        Delegates flattening to `from_dict` so there is one place that builds
        the lookup.

        Raises:
            ValueError: If the file is missing or not the expected shape. This
                is a setup concern surfaced at startup, like config loading.
        """
        vocab_path = Path(path)
        try:
            data = json.loads(vocab_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Cannot load skills vocabulary '{vocab_path}': {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"Skills vocabulary '{vocab_path}' must be a JSON object."
            )
        return cls.from_dict(data)

    def canonicalize(self, raw_skill: str) -> Optional[str]:
        """Return the canonical name for a raw skill, or None if unknown."""
        return self._alias_to_canonical.get(raw_skill.strip().lower())


def normalize_email(raw: str) -> Optional[str]:
    """Normalize an email to a trimmed, lowercased form.

    Performs a basic shape check (must contain a single '@' with text on both
    sides and a dot in the domain). Returns None for anything that is not
    email-shaped, so callers can drop it. Full RFC validation is intentionally
    out of scope.
    """
    text = raw.strip().lower()
    if text.count("@") != 1:
        return None
    local, _, domain = text.partition("@")
    if not local or "." not in domain:
        return None
    return text


def normalize_phone(
    raw: str, region: str = _DEFAULT_PHONE_REGION
) -> Optional[str]:
    """Normalize a phone string to E.164, or None if it isn't a valid number.

    Args:
        raw: The raw phone string (may contain spaces, dashes, parentheses).
        region: Fallback region for numbers without a country code.

    Returns:
        The E.164 string (e.g. "+14155550100") or None if invalid.
    """
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )


def normalize_date(raw: str) -> Optional[str]:
    """Normalize a date string to ISO-8601, preserving granularity.

    "2021" -> "2021", "March 2021" -> "2021-03", "2021-03-04" -> "2021-03-04".
    Returns None if the value cannot be parsed as a date.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        # Two fixed anchor dates reveal which components the source supplied:
        # a component that differs between the anchors was defaulted, not given.
        parsed_a = date_parser.parse(text, default=datetime(2000, 1, 1))
        parsed_b = date_parser.parse(text, default=datetime(2001, 2, 2))
    except (ParserError, ValueError, OverflowError):
        return None

    if parsed_a.year != parsed_b.year:
        return None  # no year present -> not a usable date for our schema
    if parsed_a.month != parsed_b.month:
        return parsed_a.strftime(_ISO_YEAR)
    if parsed_a.day != parsed_b.day:
        return parsed_a.strftime(_ISO_MONTH)
    return parsed_a.strftime(_ISO_FULL)


def normalize_country(raw: str) -> Optional[str]:
    """Normalize a country name or code to ISO-3166 alpha-2.

    Accepts full names ("United States"), alpha-2 ("US"), and alpha-3 ("USA").
    Returns None if the country cannot be identified.
    """
    text = raw.strip()
    if not text:
        return None

    # Exact code matches first (cheap and unambiguous).
    upper = text.upper()
    if len(upper) == 2 and pycountry.countries.get(alpha_2=upper):
        return upper
    if len(upper) == 3:
        country = pycountry.countries.get(alpha_3=upper)
        if country:
            return country.alpha_2

    # Fall back to a name lookup, tolerant of minor variations.
    try:
        matches = pycountry.countries.search_fuzzy(text)
    except LookupError:
        return None
    return matches[0].alpha_2 if matches else None


def normalize_skill(raw: str, vocabulary: SkillVocabulary) -> Optional[str]:
    """Return the canonical skill name, or None if not in the vocabulary.

    None means "unmapped": the caller keeps the raw value and flags it rather
    than discarding the candidate's stated skill.
    """
    if not raw or not raw.strip():
        return None
    return vocabulary.canonicalize(raw)
