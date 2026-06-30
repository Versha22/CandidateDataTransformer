"""Runtime configuration for the projection layer.

Per the Step 1 design, configuration controls *only* how the fixed canonical
model is projected into a consumer's output. It can select fields, rename them,
choose null-vs-omit behaviour, and toggle whether confidence and provenance are
emitted. It cannot add, remove, or reshape canonical fields.

The config is loaded and validated at startup ("fail fast"): an invalid config
raises `ConfigError` before any candidate record is touched, so a batch never
fails half-way through. Each config carries a version so an output can be
reproduced with the same projection rules.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from transformer.errors import ConfigError

# Canonical fields a config may select or rename, in a fixed, deterministic
# order. This tuple is the contract between config and models.CandidateProfile:
# listing the fields explicitly lets us reject typos at load time instead of
# silently dropping a field the user thought they had selected. The order is
# used when a config selects "all" fields, so output ordering is reproducible.
CANONICAL_FIELD_ORDER: tuple[str, ...] = (
    "candidate_id",
    "full_name",
    "emails",
    "phones",
    "skills",
    "experience",
    "education",
    "links",
    "profile_confidence",
    "needs_review",
)

CANONICAL_FIELDS: frozenset[str] = frozenset(CANONICAL_FIELD_ORDER)


class NullPolicy(str, Enum):
    """How absent canonical fields are represented in the output."""

    NULL = "null"  # emit the output key with a null value
    OMIT = "omit"  # drop the output key entirely


class ProjectionConfig(BaseModel):
    """Declarative rules for projecting a CandidateProfile into output.

    Attributes:
        version: Identifier for this config, recorded so output is reproducible.
        fields: Canonical field names to include, in output order. Empty means
            "all canonical fields", emitted in `CANONICAL_FIELD_ORDER`.
        rename: Map of canonical field name -> output key name.
        null_policy: Whether absent fields are emitted as null or omitted.
        include_confidence: Emit per-field and profile confidence when true.
        include_provenance: Emit per-field provenance entries when true.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    fields: list[str] = Field(default_factory=list)
    rename: dict[str, str] = Field(default_factory=dict)
    null_policy: NullPolicy = NullPolicy.OMIT
    include_confidence: bool = True
    include_provenance: bool = True

    @model_validator(mode="after")
    def _validate_against_canonical(self) -> "ProjectionConfig":
        """Reject any reference to a field that is not on the canonical model.

        This is the meta-schema check: it guarantees a config can only ever
        project fields that actually exist on `CandidateProfile`.
        """
        unknown_selected = set(self.fields) - CANONICAL_FIELDS
        if unknown_selected:
            raise ValueError(
                f"Config selects unknown canonical field(s): "
                f"{sorted(unknown_selected)}. "
                f"Allowed fields: {list(CANONICAL_FIELD_ORDER)}"
            )

        duplicate_check: set[str] = set()
        duplicates = {f for f in self.fields if f in duplicate_check or duplicate_check.add(f)}
        if duplicates:
            raise ValueError(
                f"Config selects duplicate field(s): {sorted(duplicates)}."
            )

        unknown_renamed = set(self.rename) - CANONICAL_FIELDS
        if unknown_renamed:
            raise ValueError(
                f"Config renames unknown canonical field(s): "
                f"{sorted(unknown_renamed)}."
            )

        return self

    @property
    def selected_fields(self) -> list[str]:
        """Canonical fields to emit, in deterministic order.

        An empty selection means "all fields", returned in
        `CANONICAL_FIELD_ORDER` so output ordering is reproducible.
        """
        if not self.fields:
            return list(CANONICAL_FIELD_ORDER)
        return list(self.fields)

    def output_key(self, canonical_field: str) -> str:
        """Return the output key for a canonical field, applying any rename."""
        return self.rename.get(canonical_field, canonical_field)


def load_config(path: str | Path) -> ProjectionConfig:
    """Load and validate a projection config from a JSON file.

    Fails fast with a clear `ConfigError` if the file is missing, is not valid
    JSON, has the wrong root type, or violates the projection meta-schema.
    Raising at startup is intentional: a bad config must never let a batch begin.

    Args:
        path: Path to the JSON config file.

    Returns:
        A validated, immutable `ProjectionConfig`.

    Raises:
        ConfigError: If the file cannot be read, parsed, or validated.
    """
    config_path = Path(path)

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file '{config_path}': {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Config file '{config_path}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Config file '{config_path}' must contain a JSON object, "
            f"got {type(data).__name__}."
        )

    try:
        return ProjectionConfig(**data)
    except ValidationError as exc:
        raise ConfigError(
            f"Config file '{config_path}' failed validation:\n{exc}"
        ) from exc
