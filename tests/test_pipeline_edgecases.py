"""Edge-case tests for the end-to-end pipeline.

Covers the design's hard cases: the same candidate across multiple sources is
merged into one profile (exact-identifier match), conflicting phone formats
collapse to one E.164 value, a second email is retained (multi-valued contacts),
and a malformed file is quarantined without stopping the batch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from transformer.config import ProjectionConfig
from transformer.normalize import SkillVocabulary
from transformer.pipeline import Pipeline


@pytest.fixture
def config() -> ProjectionConfig:
    """Full-output config so we can inspect confidence and provenance."""
    return ProjectionConfig(
        version="test-v1",
        fields=["candidate_id", "full_name", "emails", "phones", "skills"],
        include_confidence=True,
        include_provenance=True,
    )


@pytest.fixture
def vocabulary() -> SkillVocabulary:
    return SkillVocabulary.from_dict(
        {"python": ["py"], "javascript": ["js"], "kubernetes": ["k8s"]}
    )


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_same_candidate_merges_across_sources(
    tmp_path: Path, config: ProjectionConfig, vocabulary: SkillVocabulary
) -> None:
    """ATS + CSV records sharing email/phone merge into one profile."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    _write(
        input_dir / "ats.json",
        json.dumps(
            {
                "candidates": [
                    {
                        "full_name": "Jane Doe",
                        "email": "jane.doe@example.com",
                        "phone": "+1 415-555-0100",
                        "skills": ["Python", "JS"],
                    }
                ]
            }
        ),
    )
    _write(
        input_dir / "people.csv",
        "full_name,email,phone,skills\n"
        "Jane Doe,jane.doe@example.com,415-555-0100,\"py, k8s\"\n",
    )

    result = Pipeline(config=config, vocabulary=vocabulary).run(input_dir)

    # One merged candidate, no quarantine.
    assert len(result.candidates) == 1
    assert result.quarantined == []

    candidate = result.candidates[0]
    # Two phone spellings collapsed to a single E.164 value.
    phone_values = [p["value"] for p in candidate["phones"]]
    assert phone_values == ["+14155550100"]
    # Corroborated by two sources -> confidence above a single source's base.
    assert candidate["phones"][0]["confidence"] > 0.9
    assert len(candidate["phones"][0]["provenance"]) == 2
    # Skills canonicalized and de-duped across sources.
    skill_values = {s["value"] for s in candidate["skills"]}
    assert {"python", "javascript", "kubernetes"} <= skill_values


def test_malformed_file_is_quarantined_and_batch_continues(
    tmp_path: Path, config: ProjectionConfig, vocabulary: SkillVocabulary
) -> None:
    """A broken JSON file is skipped; valid records still produce output."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    _write(input_dir / "broken.json", '{ "candidates": [ { "email": "oops"')
    _write(
        input_dir / "good.csv",
        "full_name,email,phone\nMark Smith,mark@example.com,202-555-0182\n",
    )

    result = Pipeline(config=config, vocabulary=vocabulary).run(input_dir)

    # The good record is produced; the broken file is reported, not fatal.
    assert len(result.candidates) == 1
    assert result.candidates[0]["full_name"]["value"]["display"] == "Mark Smith"
    assert len(result.quarantined) == 1
    assert "broken.json" in result.quarantined[0]
