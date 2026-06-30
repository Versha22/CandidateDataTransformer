CandidateDataTransformer/
├── README.md
├── requirements.txt
├── cli.py                          # entry point: argparse, wires the pipeline
├── config/
│   ├── default_config.json         # runtime projection config (fields, rename, toggles)
│   └── skills_vocabulary.json      # canonical skill names + aliases (js -> javascript)
├── transformer/
│   ├── __init__.py
│   ├── models.py                   # Internal canonical schema (pydantic) + provenance/confidence
│   ├── config.py                   # Runtime config model + meta-schema validation (fail fast)
│   ├── pipeline.py                 # Orchestrates stages, isolates per-record failures
│   ├── detect.py                   # Input Detection: classify file -> source type
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py                 # SourceParser interface -> list[RawRecord]
│   │   ├── ats_json.py             # Structured source: ATS JSON
│   │   ├── csv_source.py           # Structured source: CSV
│   │   └── recruiter_notes.py      # Unstructured source: free-text notes (PDF/txt)
│   ├── normalize.py                # phone E.164, date ISO, country ISO-2, skills canonical
│   ├── resolution.py               # Entity Resolution: exact-first, fuzzy-above-threshold
│   ├── merge.py                    # Conflict Resolution: confidence x source_tier
│   ├── confidence.py               # base score per source + corroboration adjust
│   ├── provenance.py               # build per-field provenance entries
│   ├── projection.py               # Projection: select/rename/null-vs-omit/toggles
│   └── errors.py                   # typed exceptions + quarantine record
├── tests/
│   ├── __init__.py
│   ├── test_normalize.py           # happy path: normalization rules
│   └── test_merge_edgecases.py     # edge case: conflicting values / ambiguous merge
├── input/                          # sample structured + unstructured inputs
├── output/                         # generated canonical JSON
└── docs/
    └── design.md                   # the one-page design doc from Step 1
