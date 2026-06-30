"""Happy-path tests for the normalization rules.

These verify the four canonical normalization decisions from the design:
phones -> E.164, dates -> ISO (granularity preserved), country -> ISO alpha-2,
and skills -> canonical names. Each rule is a pure function, so each is tested
in isolation.
"""

from __future__ import annotations

import pytest

from transformer.normalize import (
    SkillVocabulary,
    normalize_country,
    normalize_date,
    normalize_email,
    normalize_phone,
    normalize_skill,
)


@pytest.fixture
def vocabulary() -> SkillVocabulary:
    """A small in-memory vocabulary, so tests don't depend on the JSON file."""
    return SkillVocabulary.from_dict(
        {
            "javascript": ["js", "node.js"],
            "kubernetes": ["k8s"],
            "python": [],
        }
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+1 415-555-0100", "+14155550100"),
        ("415-555-0100", "+14155550100"),
        ("(415) 555 0100", "+14155550100"),
    ],
)
def test_phone_formats_collapse_to_one_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw, region="US") == expected


def test_invalid_phone_returns_none() -> None:
    assert normalize_phone("not-a-phone") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2021-03-04", "2021-03-04"),
        ("March 2021", "2021-03"),
        ("2021", "2021"),
    ],
)
def test_date_granularity_is_preserved(raw: str, expected: str) -> None:
    assert normalize_date(raw) == expected


def test_unparseable_date_returns_none() -> None:
    assert normalize_date("sometime soon") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("United States", "US"),
        ("USA", "US"),
        ("us", "US"),
        ("India", "IN"),
    ],
)
def test_country_normalizes_to_alpha_2(raw: str, expected: str) -> None:
    assert normalize_country(raw) == expected


def test_email_is_lowercased_and_validated() -> None:
    assert normalize_email("  Jane.Doe@Example.COM ") == "jane.doe@example.com"
    assert normalize_email("oops") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("JS", "javascript"),
        ("node.js", "javascript"),
        ("k8s", "kubernetes"),
        ("Python", "python"),
    ],
)
def test_skill_aliases_map_to_canonical(
    raw: str, expected: str, vocabulary: SkillVocabulary
) -> None:
    assert normalize_skill(raw, vocabulary) == expected


def test_unknown_skill_returns_none(vocabulary: SkillVocabulary) -> None:
    # None signals "unmapped"; the pipeline keeps the raw value rather than drop.
    assert normalize_skill("cobol", vocabulary) is None
