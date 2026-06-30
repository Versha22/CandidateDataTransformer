# Candidate Data Transformer

Transforms candidate data from multiple structured and unstructured sources into
a single canonical profile per candidate. The output shape is controlled by a
runtime configuration file, so different consumers can be served without code
changes.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

