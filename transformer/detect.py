"""Input detection and batch parsing.

The first pipeline stage. It discovers input files, routes each one to the
parser that accepts it, and collects the resulting `RawRecord`s for the rest of
the pipeline.

Failure isolation lives here at the file boundary: if a file is unsupported or a
parser raises `ParsingError`, that file is quarantined (recorded with its cause)
and the batch continues. A single bad file never stops the run, per the Step 1
design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from transformer.errors import DetectionError, ParsingError, QuarantineError
from transformer.sources.ats_json import AtsJsonParser
from transformer.sources.base import RawRecord, SourceParser
from transformer.sources.csv_source import CsvParser
from transformer.sources.recruiter_notes import RecruiterNotesParser


def default_parsers() -> list[SourceParser]:
    """Return the parsers supported by this build, in routing order.

    Order matters only when two parsers could claim the same file; today each
    parser owns a distinct extension, so the order is for determinism rather
    than disambiguation.
    """
    return [AtsJsonParser(), CsvParser(), RecruiterNotesParser()]


@dataclass
class DetectionResult:
    """Outcome of parsing a batch of input files.

    Attributes:
        records: All raw records successfully parsed across every input file.
        quarantined: One entry per file that could not be detected or parsed,
            kept so the run can report what was skipped and why.
    """

    records: list[RawRecord] = field(default_factory=list)
    quarantined: list[QuarantineError] = field(default_factory=list)


class InputDetector:
    """Routes input files to parsers and parses a whole batch.

    The detector holds the available parsers and does not itself read file
    contents; each parser decides whether it can handle a file via `can_parse`.
    """

    def __init__(self, parsers: list[SourceParser] | None = None) -> None:
        """Create a detector.

        Args:
            parsers: Parsers to route between. Defaults to `default_parsers()`.
        """
        self._parsers = parsers if parsers is not None else default_parsers()

    def select_parser(self, path: Path) -> SourceParser:
        """Return the first parser that accepts `path`.

        Args:
            path: Path to an input file.

        Returns:
            The matching parser.

        Raises:
            DetectionError: If no parser can handle the file.
        """
        for parser in self._parsers:
            if parser.can_parse(path):
                return parser
        raise DetectionError(
            f"No parser supports file '{path}' (suffix '{path.suffix}')."
        )

    def parse_file(self, path: Path) -> list[RawRecord]:
        """Detect the source type of one file and parse it.

        Raises:
            DetectionError: If no parser supports the file.
            ParsingError: If the matching parser fails on the file's content.
        """
        parser = self.select_parser(path)
        return parser.parse(path)

    def parse_batch(self, input_dir: Path) -> DetectionResult:
        """Parse every file in a directory, isolating per-file failures.

        Files that cannot be detected or parsed are quarantined with their
        cause; all other files still contribute their records.

        Args:
            input_dir: Directory containing input files.

        Returns:
            A `DetectionResult` with parsed records and quarantined files.

        Raises:
            DetectionError: If `input_dir` does not exist or is not a directory.
                This is a setup error, not a per-file failure, so it aborts.
        """
        if not input_dir.is_dir():
            raise DetectionError(
                f"Input path '{input_dir}' is not an existing directory."
            )

        result = DetectionResult()
        for path in self._iter_input_files(input_dir):
            try:
                result.records.extend(self.parse_file(path))
            except (DetectionError, ParsingError) as exc:
                result.quarantined.append(
                    QuarantineError(
                        f"Skipped input file '{path}'",
                        source=str(path),
                        cause=exc,
                    )
                )
        return result

    @staticmethod
    def _iter_input_files(input_dir: Path) -> list[Path]:
        """Return regular files in `input_dir`, sorted for deterministic order.

        Hidden files (dotfiles) are skipped so editor/OS artifacts such as
        `.DS_Store` don't reach the parsers or the quarantine list.
        """
        files = [
            path
            for path in input_dir.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ]
        return sorted(files)
