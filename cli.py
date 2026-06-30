"""Command-line entry point for the Candidate Data Transformer.

Usage:
    python cli.py \
        --input input/ \
        --config config/default_config.json \
        --output output/default_output.json

The CLI owns the startup-failure boundary. Loading the projection config and the
skill vocabulary happens here and aborts the run with a clear message if either
is invalid, before any candidate record is processed. Everything after that is
delegated to the `Pipeline`, which isolates per-record failures into the output's
quarantine report.

Exit codes:
    0  success (the batch ran; individual records may have been quarantined)
    1  unexpected failure
    2  startup failure (bad config, missing vocabulary, bad input/output path)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transformer.config import load_config
from transformer.errors import CandidateTransformerError, ConfigError
from transformer.normalize import SkillVocabulary
from transformer.pipeline import Pipeline

# Default location of the skill vocabulary, alongside the other config files.
_DEFAULT_VOCAB_PATH = Path("config/skills_vocabulary.json")

# Exit codes (documented in the module docstring).
_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_STARTUP = 2


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface."""
    parser = argparse.ArgumentParser(
        prog="candidate-transformer",
        description="Transform multi-source candidate data into canonical "
        "profiles, shaped by a runtime projection config.",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Directory of input files (CSV, ATS JSON, recruiter .txt).",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to the JSON projection config.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path to write the output JSON document.",
    )
    parser.add_argument(
        "--vocabulary",
        type=Path,
        default=_DEFAULT_VOCAB_PATH,
        help=f"Path to the skill vocabulary JSON "
        f"(default: {_DEFAULT_VOCAB_PATH}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code.

    Args:
        argv: Optional argument list (for testing); defaults to sys.argv.

    Returns:
        An exit code: 0 success, 2 startup failure, 1 unexpected failure.
    """
    args = build_parser().parse_args(argv)

    # --- Startup boundary: load config + vocabulary, fail fast and clearly. ---
    try:
        config = load_config(args.config)
        vocabulary = SkillVocabulary.from_file(args.vocabulary)
    except (ConfigError, ValueError) as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return _EXIT_STARTUP

    if not args.input.is_dir():
        print(
            f"Startup error: input '{args.input}' is not a directory.",
            file=sys.stderr,
        )
        return _EXIT_STARTUP

    # --- Run the batch. Per-record failures are isolated by the pipeline. ---
    try:
        result = Pipeline(config=config, vocabulary=vocabulary).run(args.input)
        _write_output(args.output, result.to_envelope())
    except CandidateTransformerError as exc:
        # A package-level error that escaped per-record isolation: report it.
        print(f"Run failed: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    except OSError as exc:
        print(f"Could not write output '{args.output}': {exc}", file=sys.stderr)
        return _EXIT_ERROR

    _print_summary(result, args.output)
    return _EXIT_OK


def _write_output(path: Path, envelope: dict) -> None:
    """Write the output envelope as pretty-printed UTF-8 JSON.

    Creates the parent directory if needed so a fresh checkout's `output/`
    doesn't have to pre-exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _print_summary(result, output_path: Path) -> None:
    """Print a short, human-readable run summary to stdout."""
    print(
        f"Wrote {len(result.candidates)} candidate profile(s) to "
        f"'{output_path}'."
    )
    if result.quarantined:
        print(f"Quarantined {len(result.quarantined)} input(s)/record(s):")
        for message in result.quarantined:
            print(f"  - {message}")


if __name__ == "__main__":
    raise SystemExit(main())
