"""Internal canonical schema for the Candidate Data Transformer.

This module defines the *fixed* internal model (per the Step 1 design). It is
intentionally rich: every canonical value is wrapped in an `Attribute`, which
carries the value together with its provenance and confidence. The runtime
projection layer later reads these objects to produce consumer-specific output,
but it never mutates the canonical model itself.

Nothing here is configurable at runtime. Adding a new canonical field is a
deliberate code change, by design.

Serialization contract:
    All output serialization MUST use `model_dump(mode="json")` (or
    `model_dump_json()`). `Provenance.timestamp` is a `datetime`, which is not
    JSON-serializable in Python mode; `mode="json"` renders it as an ISO-8601
    string. The projection/pipeline layers are responsible for honouring this.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# Generic value type carried by an Attribute (str, normalized date string, etc.).
T = TypeVar("T")

# Confidence bands. These only affect the human-readable High/Medium/Low label;
# the numeric score drives the actual conflict-resolution math. They live here
# as named constants so the cut-offs are visible and tunable in one place,
# rather than being magic numbers buried in a method.
HIGH_CONFIDENCE_THRESHOLD: float = 0.75
MEDIUM_CONFIDENCE_THRESHOLD: float = 0.45


class SourceType(str, Enum):
    """Origin of a piece of data.

    The order here has no meaning; merge priority lives in configuration
    (default tier ATS >= Resume >= GitHub >= Notes), not in this enum.
    """

    ATS = "ats"
    RESUME = "resume"
    GITHUB = "github"
    RECRUITER_NOTES = "recruiter_notes"
    CSV = "csv"
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    """How a value was obtained from its source.

    Used by the confidence model: a value read from a structured field is more
    trustworthy than one pulled out of free text.
    """

    STRUCTURED_FIELD = "structured_field"  # direct key in JSON/CSV
    TEXT_EXTRACTION = "text_extraction"    # parsed out of unstructured notes
    DERIVED = "derived"                    # computed/normalized from other input


class ConfidenceLevel(str, Enum):
    """Coarse confidence bands used for human-readable output and thresholds."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LinkType(str, Enum):
    """Typed links so projection can filter by kind (design: links are typed)."""

    GITHUB = "github"
    LINKEDIN = "linkedin"
    PORTFOLIO = "portfolio"
    OTHER = "other"


class Provenance(BaseModel):
    """Where a single value came from and how trustworthy it is.

    One of these is recorded for *each* contributing source of a value. Losing
    values in a conflict are retained (marked superseded) so a merge can be
    explained and replayed without re-ingesting.
    """

    model_config = ConfigDict(frozen=True)

    source: SourceType
    method: ExtractionMethod
    confidence: float = Field(ge=0.0, le=1.0)
    raw_value: Optional[str] = Field(
        default=None,
        description="Original value before normalization, kept for audit.",
    )
    timestamp: Optional[datetime] = Field(
        default=None,
        description="When the source record was produced, if known. Used as a "
        "tie-breaker during conflict resolution. Serialized as ISO-8601 via "
        "model_dump(mode='json').",
    )
    superseded: bool = Field(
        default=False,
        description="True if this value lost a conflict but is kept for audit.",
    )


class Attribute(BaseModel, Generic[T]):
    """A single canonical value plus its full provenance.

    `value` is the winning, normalized value. `confidence` is the final score
    for that value (after corroboration). `provenance` holds an entry per
    contributing source, including superseded ones.
    """

    value: T
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: list[Provenance] = Field(default_factory=list)

    @property
    def level(self) -> ConfidenceLevel:
        """Map the numeric score to a coarse band for readable output."""
        if self.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            return ConfidenceLevel.HIGH
        if self.confidence >= MEDIUM_CONFIDENCE_THRESHOLD:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW


class Name(BaseModel):
    """Structured name plus the raw display string.

    Names don't split reliably, so we keep the original display string and only
    populate given/family when a source provides them cleanly.
    """

    display: str
    given: Optional[str] = None
    family: Optional[str] = None


class DateRange(BaseModel):
    """Normalized, possibly open-ended date range (ISO-8601 strings).

    Partial dates are allowed (e.g. "2021" or "2021-03"). An open `end` with
    `is_current=True` models an ongoing role or study.
    """

    start: Optional[str] = None
    end: Optional[str] = None
    is_current: bool = False


class Experience(BaseModel):
    """One employment record.

    `company`/`title` are sourced values, so they are wrapped in `Attribute`
    and carry provenance. `dates` deliberately does not: date ranges are not
    conflict-resolved per the design, so they are kept as a plain normalized
    range rather than a per-field provenance-bearing attribute.
    """

    company: Attribute[str]
    title: Optional[Attribute[str]] = None
    dates: Optional[DateRange] = None


class Education(BaseModel):
    """One education record.

    Same asymmetry as `Experience`: `institution`/`degree` are sourced values
    with provenance; `dates` is a plain normalized range (not conflict-resolved).
    """

    institution: Attribute[str]
    degree: Optional[Attribute[str]] = None
    dates: Optional[DateRange] = None


class Link(BaseModel):
    """A typed external link.

    `type` is a derived classification (not a sourced value) so it is a plain
    enum; `url` is the sourced value and therefore carries provenance.
    """

    type: LinkType
    url: Attribute[str]


class CandidateProfile(BaseModel):
    """The canonical candidate profile (one per resolved candidate).

    Contact fields are lists because a candidate legitimately has several emails
    or phones; collapsing them would lose signal and break de-duplication.
    `profile_confidence` is computed from identity attributes (name, email,
    phone) so a missing portfolio link doesn't drag the score down.
    """

    candidate_id: str = Field(
        description="Internal stable UUID. Never a source key, since emails and "
        "phones change but the identity should not."
    )
    full_name: Optional[Attribute[Name]] = None
    emails: list[Attribute[str]] = Field(default_factory=list)
    phones: list[Attribute[str]] = Field(default_factory=list)
    skills: list[Attribute[str]] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)

    profile_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    needs_review: bool = Field(
        default=False,
        description="Set when a record is low-confidence or part of an "
        "ambiguous match that was not merged.",
    )
