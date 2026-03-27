#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p _da_v17_run
# Activity id: default is DEFAULT_DA_ACTIVITY_ID in da_layer_pdf_pipeline.py; override with:
#   export DA_ACTIVITY_ID='YourNick.LayerPdfExportActivity+prod'

if [[ ! -f .aps ]]; then
  echo "Missing .aps in $ROOT — copy ../.aps or create client_credentials = 'ID:SECRET' (see SETUP.txt)." >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [[ ! -f example.dwg ]]; then
  echo "Missing example.dwg in $ROOT" >&2
  exit 1
fi

exec .venv/bin/python da_layer_pdf_pipeline.py \
  --input example.dwg \
  --output _da_v17_run/layer_pdfs.zip \
  --aps .aps
