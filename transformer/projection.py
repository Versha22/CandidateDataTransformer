"""Projection: render a canonical profile into configured output.

The final pipeline stage before serialization. Per the Step 1 design, this is
the *only* place output shape is decided, and it is driven entirely by the
runtime `ProjectionConfig`:

* field selection and ordering,
* field renaming,
* null-vs-omit for absent fields,
* whether per-field confidence is emitted,
* whether per-field provenance is emitted.

`include_confidence` governs all confidence output, including the top-level
`profile_confidence`: when it is False, `profile_confidence` is treated as
absent regardless of field selection, so disabling confidence never leaks a
confidence value.

Projection reads the canonical `CandidateProfile` but never mutates it. It
returns a plain, JSON-serializable dict (datetimes already rendered as ISO-8601
strings) so the caller can write it with `json.dump` directly.
"""

from __future__ import annotations

from typing import Any

from transformer.config import NullPolicy, ProjectionConfig
from transformer.errors import ProjectionError
from transformer.models import (
    Attribute,
    CandidateProfile,
    Education,
    Experience,
    Link,
)

# Canonical fields by how they render.
_SCALAR_ATTRIBUTE_FIELDS: frozenset[str] = frozenset({"full_name"})
_LIST_ATTRIBUTE_FIELDS: frozenset[str] = frozenset({"emails", "phones", "skills"})
_RECORD_FIELDS: frozenset[str] = frozenset({"experience", "education", "links"})
_PLAIN_FIELDS: frozenset[str] = frozenset(
    {"candidate_id", "profile_confidence", "needs_review"}
)


def project(profile: CandidateProfile, config: ProjectionConfig) -> dict[str, Any]:
    """Render a canonical profile into a configured output dict.

    Args:
        profile: The merged canonical candidate profile.
        config: The validated runtime projection configuration.

    Returns:
        A JSON-serializable dict shaped according to `config`.

    Raises:
        ProjectionError: If a selected field cannot be rendered.
    """
    output: dict[str, Any] = {}
    for canonical_field in config.selected_fields:
        try:
            present, rendered = _render_field(profile, canonical_field, config)
        except Exception as exc:  # defensive: turn any render fault into ours
            raise ProjectionError(
                f"Failed to project field '{canonical_field}': {exc}"
            ) from exc

        key = config.output_key(canonical_field)
        if present:
            output[key] = rendered
        elif config.null_policy == NullPolicy.NULL:
            output[key] = None
        # OMIT: drop the key entirely.
    return output


def _render_field(
    profile: CandidateProfile, canonical_field: str, config: ProjectionConfig
) -> tuple[bool, Any]:
    """Render one canonical field.

    Returns a (present, value) pair. `present` is False when the profile has no
    value for the field, so the caller can apply the null-vs-omit policy.
    """
    value = getattr(profile, canonical_field)

    # profile_confidence is confidence output: suppress it entirely when the
    # confidence toggle is off, so the toggle governs all confidence data.
    if canonical_field == "profile_confidence":
        if not config.include_confidence or value is None:
            return (False, None)
        return (True, value)

    if canonical_field in _PLAIN_FIELDS:
        return (value is not None, value)

    if canonical_field in _SCALAR_ATTRIBUTE_FIELDS:
        if value is None:
            return (False, None)
        return (True, _render_attribute(value, config))

    if canonical_field in _LIST_ATTRIBUTE_FIELDS:
        if not value:
            return (False, None)
        return (True, [_render_attribute(item, config) for item in value])

    if canonical_field in _RECORD_FIELDS:
        if not value:
            return (False, None)
        return (True, _render_records(value, config))

    # Unreachable: config validation already rejects unknown fields.
    raise ProjectionError(f"Unknown canonical field '{canonical_field}'.")


def _render_attribute(attribute: Attribute, config: ProjectionConfig) -> Any:
    """Render an Attribute, honoring the confidence/provenance toggles.

    When both metadata toggles are off, the attribute collapses to its bare
    value so consumers that only want data get a clean shape. Otherwise it
    becomes an object carrying the value plus the enabled metadata.
    """
    rendered_value = _render_value(attribute.value)

    if not config.include_confidence and not config.include_provenance:
        return rendered_value

    result: dict[str, Any] = {"value": rendered_value}
    if config.include_confidence:
        result["confidence"] = attribute.confidence
        result["confidence_level"] = attribute.level.value
    if config.include_provenance:
        # mode="json" renders datetimes as ISO-8601 strings (serialization
        # contract from models.py) and enums as their string values.
        result["provenance"] = [
            entry.model_dump(mode="json") for entry in attribute.provenance
        ]
    return result


def _render_value(value: Any) -> Any:
    """Render an attribute's inner value to a JSON-friendly form.

    Most values are plain strings; the structured `Name` value is dumped to a
    dict. Pydantic models are dumped in JSON mode for safe serialization.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _render_records(
    records: list[Experience | Education | Link], config: ProjectionConfig
) -> list[dict[str, Any]]:
    """Render experience/education/link records.

    These nest `Attribute`s (e.g. `Experience.company`), so each is rendered
    through `_render_attribute` to honor the same metadata toggles, while plain
    sub-fields (dates, link type) are dumped directly.

    Relies on Pydantic v2 `BaseModel.__iter__` yielding (field_name, value)
    pairs; pinned here so a future Pydantic change is caught by tests.
    """
    rendered: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {}
        for name, value in record:
            if isinstance(value, Attribute):
                item[name] = _render_attribute(value, config)
            elif value is None:
                continue
            elif hasattr(value, "model_dump"):
                item[name] = value.model_dump(mode="json")
            else:
                item[name] = value
        rendered.append(item)
    return rendered
