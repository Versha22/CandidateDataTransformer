"""Custom exceptions for the Candidate Data Transformer.

The hierarchy mirrors the Step 1 design's failure model, which distinguishes
two kinds of failure:

* Startup failures (e.g. an invalid config) must abort before any record is
  processed. `ConfigError` covers these.
* Per-record failures (bad parse, un-normalizable value, ambiguous merge, etc.)
  must be isolated: the offending record is quarantined and the batch
  continues. These all derive from `RecordError`, so the pipeline can catch the
  whole family with a single `except RecordError`.

All exceptions derive from `CandidateTransformerError`, so callers can catch
everything this package raises without catching unrelated built-ins.
"""

from __future__ import annotations

from typing import Optional


class CandidateTransformerError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(CandidateTransformerError):
    """Raised when the runtime configuration is missing or invalid.

    This is a startup failure: it is raised before the batch begins and is not
    recoverable per-record, so it deliberately does not derive from
    `RecordError`.
    """


class RecordError(CandidateTransformerError):
    """Base class for failures that are scoped to a single source record.

    Errors in this family are recoverable at the batch level: the pipeline
    quarantines the affected record and continues processing the rest.
    """


class DetectionError(RecordError):
    """Raised when an input file's source type cannot be determined."""


class ParsingError(RecordError):
    """Raised when a source file cannot be parsed into raw records."""


class ValidationError(RecordError):
    """Raised when a record fails schema or field validation."""


class NormalizationError(RecordError):
    """Raised when a value cannot be normalized to its canonical form."""


class EntityResolutionError(RecordError):
    """Raised when grouping records to a single candidate fails."""


class MergeError(RecordError):
    """Raised when conflict resolution cannot produce a single value."""


class ProjectionError(RecordError):
    """Raised when a validated profile cannot be projected to output."""


class QuarantineError(RecordError):
    """Wraps a record that was set aside after an unrecoverable per-record error.

    Carries the original cause and a best-effort identifier for the source so
    the quarantine log is actionable (which file/record failed and why).
    """

    def __init__(
        self,
        message: str,
        *,
        source: Optional[str] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        """Initialize a quarantine error.

        Args:
            message: Human-readable description of the failure.
            source: Best-effort identifier of the source (e.g. file path).
            cause: The original exception that triggered quarantine.
        """
        super().__init__(message)
        self.source = source
        self.cause = cause

    def __str__(self) -> str:
        base = super().__str__()
        if self.source is not None:
            base = f"{base} (source: {self.source})"
        if self.cause is not None:
            base = f"{base} [cause: {type(self.cause).__name__}: {self.cause}]"
        return base
