# Candidate Data Transformer

Transforms candidate data from multiple structured and unstructured sources into
a single canonical profile per candidate. The output shape is controlled by a
runtime configuration file, so different consumers can be served without code
changes.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

Requires Python 3.11+.
Folder structure



CandidateDataTransformer/
├── cli.py                  # entry point
├── config/
│   ├── default_config.json # full output (confidence + provenance)
│   ├── slim_config.json    # bare values, no metadata
│   └── skills_vocabulary.json
├── transformer/
│   ├── models.py           # fixed internal canonical schema
│   ├── config.py           # runtime projection config + validation
│   ├── errors.py           # exception hierarchy (abort vs isolate)
│   ├── detect.py           # input detection + batch parsing
│   ├── normalize.py        # phone/date/country/email/skill rules
│   ├── confidence.py       # rule-based confidence scoring
│   ├── provenance.py       # per-field provenance + attribute assembly
│   ├── resolution.py       # entity resolution (exact-first, fuzzy-above-threshold)
│   ├── merge.py            # conflict resolution
│   ├── projection.py       # config-driven output + serialization
│   ├── pipeline.py         # orchestration
│   └── sources/            # ATS JSON, CSV, recruiter notes parsers
├── tests/
├── input/                  # sample inputs
└── output/

Run



python3 cli.py \
    --input input \
    --config config/default_config.json \
    --output output/result.json

Exit codes: 0 success, 2 startup failure (bad config / vocabulary / input),
1 unexpected runtime failure.
Example output
Running the command above against the sample input/ directory:



python3 cli.py \
    --input input \
    --config config/default_config.json \
    --output output/result.json




Wrote 4 candidate profile(s) to 'output/result.json'.

The generated output/result.json is an envelope with two keys:

candidates — the canonical profiles. Each profile has a generated
candidate_id, a name, lists of emails, phones, and skills, a
profile_confidence, and a needs_review flag. With default_config.json
every value is an object carrying its value, a confidence score and
confidence_level, and a provenance list recording which source(s) it came
from. Records for the same candidate seen in more than one source are merged:
duplicate phone formats collapse to a single E.164 value, multiple emails are
all kept, and agreement across sources raises confidence.
quarantined — human-readable messages for any input file or record that
could not be processed (e.g. malformed JSON). These are skipped so the rest of
the batch still completes.

Abbreviated shape of one candidate:



{
  "candidates": [
    {
      "candidate_id": "…uuid…",
      "name": {
        "value": { "display": "Jane Doe", "given": "Jane", "family": "Doe" },
        "confidence": 0.9,
        "confidence_level": "high",
        "provenance": [ { "source": "ats", "method": "structured_field", "…": "…" } ]
      },
      "emails": [
        { "value": "jane.doe@example.com", "confidence": 0.95, "provenance": [ "…" ] }
      ],
      "phones": [
        { "value": "+14155550100", "confidence": 0.95, "provenance": [ "…", "…" ] }
      ],
      "skills": [ { "value": "python", "…": "…" }, { "value": "javascript", "…": "…" } ],
      "profile_confidence": 0.93,
      "needs_review": false
    }
  ],
  "quarantined": [ "Skipped input file 'input/malformed.json' … [cause: …]" ]
}

For a slimmer output (bare values, no confidence or provenance) run the same
command with config/slim_config.json. Both configs project the same canonical
profiles into different shapes, without any code change.
Running tests



python3 -m pytest

All 20 tests pass:



20 passed

Tests cover the normalization rules (happy path) and the end-to-end pipeline
(merge, conflict resolution, and quarantine of malformed input).
Configuration
Configuration controls projection only; the internal canonical schema is fixed.



Key
Effect



version
Recorded for reproducibility


fields
Canonical fields to emit, in order (empty = all)


rename
Map canonical field -> output key


null_policy
omit or null for absent fields


include_confidence
Emit confidence (and profile_confidence)


include_provenance
Emit per-field provenance


Design decisions

Two schemas. A fixed internal canonical model (rich, with per-field
provenance and confidence) and a config-driven output projection. The model is
stable; only the projection changes per consumer.
Provenance everywhere. Every value records source, method, confidence,
timestamp, and raw value; losing values in a conflict are kept and marked
superseded so merges can be explained.
Rule-based throughout. Entity resolution and confidence are heuristic and
explainable, not learned. Weights and thresholds live in named constants.
Failure isolation. A bad file or record is quarantined; the batch
continues. Only an invalid config or vocabulary aborts (at startup).

Assumptions

Batch processing of a flat input directory; UTF-8 input.
One file is one source type; one CSV row / notes file is one candidate.
Default phone region is US for numbers lacking a country code.
Skill matching is exact (alias-based); unknown skills are kept as raw.

Edge cases

Conflicting phone formats collapse to one E.164 value.
Multiple emails/phones are all retained with their own provenance.
Same candidate across sources is merged (shared email/phone); same name with
conflicting identifiers is kept separate and flagged needs_review.
Malformed JSON / unreadable file is quarantined.
Unknown skill / unparseable phone is kept raw or dropped, never fatal.




